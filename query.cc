/*
 * Copyright (C) 2015-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#include <limits>
#include "query-request.hh"
#include "query-result.hh"
#include "query-result-writer.hh"
#include "query-result-set.hh"
#include "to_string.hh"
#include "bytes.hh"
#include "mutation_partition_serializer.hh"
#include "query-result-reader.hh"
#include "query_result_merger.hh"
#include "partition_slice_builder.hh"

namespace query {

static logging::logger qlogger("query");

constexpr size_t result_memory_limiter::minimum_result_size;
constexpr size_t result_memory_limiter::maximum_result_size;
constexpr size_t result_memory_limiter::unlimited_result_size;

thread_local semaphore result_memory_tracker::_dummy { 0 };

const dht::partition_range full_partition_range = dht::partition_range::make_open_ended_both_sides();
const clustering_range full_clustering_range = clustering_range::make_open_ended_both_sides();

std::ostream& operator<<(std::ostream& out, const specific_ranges& s);

std::ostream& operator<<(std::ostream& out, const partition_slice& ps) {
    out << "{"
        << "regular_cols=[" << join(", ", ps.regular_columns) << "]"
        << ", static_cols=[" << join(", ", ps.static_columns) << "]"
        << ", rows=[" << join(", ", ps._row_ranges) << "]"
        ;
    if (ps._specific_ranges) {
        out << ", specific=[" << *ps._specific_ranges << "]";
    }
    out << ", options=" << format("{:x}", ps.options.mask()); // FIXME: pretty print options
    out << ", cql_format=" << ps.cql_format();
    out << ", partition_row_limit=" << ps.partition_row_limit();
    return out << "}";
}

std::ostream& operator<<(std::ostream& out, const read_command& r) {
    return out << "read_command{"
        << "cf_id=" << r.cf_id
        << ", version=" << r.schema_version
        << ", slice=" << r.slice << ""
        << ", limit=" << r.get_row_limit()
        << ", timestamp=" << r.timestamp.time_since_epoch().count()
        << ", partition_limit=" << r.partition_limit
        << ", query_uuid=" << r.query_uuid
        << ", is_first_page=" << r.is_first_page
        << ", read_timestamp=" << r.read_timestamp
        << "}";
}

std::ostream& operator<<(std::ostream& out, const forward_request::reduction_type& r) {
    out << "reduction_type{";
    switch (r) {
        case forward_request::reduction_type::count:
            out << "count";
            break;
    }
    return out << "}";
}

std::ostream& operator<<(std::ostream& out, const forward_request& r) {
    auto ms = std::chrono::time_point_cast<std::chrono::milliseconds>(r.timeout).time_since_epoch().count();

    return out << "forward_request{"
        << "reduction_types=[" << join(",", r.reduction_types) << "]"
        << ", cmd=" << r.cmd
        << ", pr=" << r.pr
        << ", cl=" << r.cl
        << ", timeout(ms)=" << ms << "}";
}


std::ostream& operator<<(std::ostream& out, const specific_ranges& s) {
    return out << "{" << s._pk << " : " << join(", ", s._ranges) << "}";
}

void trim_clustering_row_ranges_to(const schema& s, clustering_row_ranges& ranges, position_in_partition_view pos, bool reversed) {
    auto cmp = [reversed, cmp = position_in_partition::composite_tri_compare(s)] (const auto& a, const auto& b) {
        return reversed ? cmp(b, a) : cmp(a, b);
    };
    auto start_bound = [reversed] (const auto& range) -> position_in_partition_view {
        return reversed ? position_in_partition_view::for_range_end(range) : position_in_partition_view::for_range_start(range);
    };
    auto end_bound = [reversed] (const auto& range) -> position_in_partition_view {
        return reversed ? position_in_partition_view::for_range_start(range) : position_in_partition_view::for_range_end(range);
    };

    auto it = ranges.begin();
    while (it != ranges.end()) {
        if (cmp(end_bound(*it), pos) <= 0) {
            it = ranges.erase(it);
            continue;
        } else if (cmp(start_bound(*it), pos) <= 0) {
            assert(cmp(pos, end_bound(*it)) < 0);
            auto r = reversed ?
                clustering_range(it->start(), clustering_range::bound(pos.key(), pos.get_bound_weight() != bound_weight::before_all_prefixed)) :
                clustering_range(clustering_range::bound(pos.key(), pos.get_bound_weight() != bound_weight::after_all_prefixed), it->end());
            *it = std::move(r);
        }
        ++it;
    }
}

void trim_clustering_row_ranges_to(const schema& s, clustering_row_ranges& ranges, const clustering_key& key, bool reversed) {
    if (key.is_full(s)) {
        return trim_clustering_row_ranges_to(s, ranges,
                reversed ? position_in_partition_view::before_key(key) : position_in_partition_view::after_key(key), reversed);
    }
    auto full_key = key;
    clustering_key::make_full(s, full_key);
    return trim_clustering_row_ranges_to(s, ranges,
            reversed ? position_in_partition_view::after_key(full_key) : position_in_partition_view::before_key(full_key), reversed);
}


clustering_range reverse(const clustering_range& range) {
    if (range.is_singular()) {
        return range;
    }
    return clustering_range(range.end(), range.start());
}


static void reverse_clustering_ranges_bounds(clustering_row_ranges& ranges) {
    for (auto& range : ranges) {
        range = reverse(range);
    }
}

partition_slice legacy_reverse_slice_to_native_reverse_slice(const schema& schema, partition_slice slice) {
    return partition_slice_builder(schema, std::move(slice))
        .mutate_ranges([] (clustering_row_ranges& ranges) { reverse_clustering_ranges_bounds(ranges); })
        .mutate_specific_ranges([] (specific_ranges& ranges) { reverse_clustering_ranges_bounds(ranges.ranges()); })
        .build();
}

partition_slice native_reverse_slice_to_legacy_reverse_slice(const schema& schema, partition_slice slice) {
    // They are the same, we give them different names to express intent
    return legacy_reverse_slice_to_native_reverse_slice(schema, std::move(slice));
}

partition_slice reverse_slice(const schema& schema, partition_slice slice) {
    return partition_slice_builder(schema, std::move(slice))
        .mutate_ranges([] (clustering_row_ranges& ranges) {
            std::reverse(ranges.begin(), ranges.end());
            reverse_clustering_ranges_bounds(ranges);
        })
        .mutate_specific_ranges([] (specific_ranges& sranges) {
            auto& ranges = sranges.ranges();
            std::reverse(ranges.begin(), ranges.end());
            reverse_clustering_ranges_bounds(ranges);
        })
        .with_option_toggled<partition_slice::option::reversed>()
        .build();
}

partition_slice half_reverse_slice(const schema& schema, partition_slice slice) {
    return partition_slice_builder(schema, std::move(slice))
        .mutate_ranges([] (clustering_row_ranges& ranges) {
            std::reverse(ranges.begin(), ranges.end());
        })
        .mutate_specific_ranges([] (specific_ranges& sranges) {
            auto& ranges = sranges.ranges();
            std::reverse(ranges.begin(), ranges.end());
        })
        .with_option_toggled<partition_slice::option::reversed>()
        .build();
}

partition_slice::partition_slice(clustering_row_ranges row_ranges,
    query::column_id_vector static_columns,
    query::column_id_vector regular_columns,
    option_set options,
    std::unique_ptr<specific_ranges> specific_ranges,
    cql_serialization_format cql_format,
    uint32_t partition_row_limit_low_bits,
    uint32_t partition_row_limit_high_bits)
    : _row_ranges(std::move(row_ranges))
    , static_columns(std::move(static_columns))
    , regular_columns(std::move(regular_columns))
    , options(options)
    , _specific_ranges(std::move(specific_ranges))
    , _cql_format(std::move(cql_format))
    , _partition_row_limit_low_bits(partition_row_limit_low_bits)
    , _partition_row_limit_high_bits(partition_row_limit_high_bits)
{}

partition_slice::partition_slice(clustering_row_ranges row_ranges,
    query::column_id_vector static_columns,
    query::column_id_vector regular_columns,
    option_set options,
    std::unique_ptr<specific_ranges> specific_ranges,
    cql_serialization_format cql_format,
    uint64_t partition_row_limit)
    : partition_slice(std::move(row_ranges), std::move(static_columns), std::move(regular_columns), options,
            std::move(specific_ranges), std::move(cql_format), static_cast<uint32_t>(partition_row_limit),
            static_cast<uint32_t>(partition_row_limit >> 32))
{}

partition_slice::partition_slice(clustering_row_ranges ranges, const schema& s, const column_set& columns, option_set options)
    : partition_slice(ranges, query::column_id_vector{}, query::column_id_vector{}, options)
{
    regular_columns.reserve(columns.count());
    for (ordinal_column_id id = columns.find_first(); id != column_set::npos; id = columns.find_next(id)) {
        const column_definition& def = s.column_at(id);
        if (def.is_static()) {
            static_columns.push_back(def.id);
        } else if (def.is_regular()) {
            regular_columns.push_back(def.id);
        } // else clustering or partition key column - skip, these are controlled by options
    }
}

partition_slice::partition_slice(partition_slice&&) = default;

partition_slice& partition_slice::operator=(partition_slice&& other) noexcept = default;

// Only needed because selection_statement::execute does copies of its read_command
// in the map-reduce op.
partition_slice::partition_slice(const partition_slice& s)
    : _row_ranges(s._row_ranges)
    , static_columns(s.static_columns)
    , regular_columns(s.regular_columns)
    , options(s.options)
    , _specific_ranges(s._specific_ranges ? std::make_unique<specific_ranges>(*s._specific_ranges) : nullptr)
    , _cql_format(s._cql_format)
    , _partition_row_limit_low_bits(s._partition_row_limit_low_bits)
    , _partition_row_limit_high_bits(s._partition_row_limit_high_bits)
{}

partition_slice::~partition_slice()
{}

const clustering_row_ranges& partition_slice::row_ranges(const schema& s, const partition_key& k) const {
    auto* r = _specific_ranges ? _specific_ranges->range_for(s, k) : nullptr;
    return r ? *r : _row_ranges;
}

void partition_slice::set_range(const schema& s, const partition_key& k, clustering_row_ranges range) {
    if (!_specific_ranges) {
        _specific_ranges = std::make_unique<specific_ranges>(k, std::move(range));
    } else {
        _specific_ranges->add(s, k, std::move(range));
    }
}

void partition_slice::clear_range(const schema& s, const partition_key& k) {
    if (_specific_ranges && _specific_ranges->contains(s, k)) {
        // just in case someone changes the impl above,
        // we should do actual remove if specific_ranges suddenly
        // becomes an actual map
        assert(_specific_ranges->size() == 1);
        _specific_ranges = nullptr;
    }
}

clustering_row_ranges partition_slice::get_all_ranges() const {
    auto all_ranges = default_row_ranges();
    const auto& specific_ranges = get_specific_ranges();
    if (specific_ranges) {
        all_ranges.insert(all_ranges.end(), specific_ranges->ranges().begin(), specific_ranges->ranges().end());
    }
    return all_ranges;
}

sstring
result::pretty_print(schema_ptr s, const query::partition_slice& slice) const {
    std::ostringstream out;
    out << "{ result: " << result_set::from_raw_result(s, slice, *this);
    out << " digest: ";
    if (_digest) {
        out << std::hex << std::setw(2);
        for (auto&& c : _digest->get()) {
            out << unsigned(c) << " ";
        }
    } else {
        out << "{}";
    }
    out << ", short_read=" << is_short_read() << " }";
    return out.str();
}

query::result::printer
result::pretty_printer(schema_ptr s, const query::partition_slice& slice) const {
    return query::result::printer{s, slice, *this};
}

std::ostream& operator<<(std::ostream& os, const query::result::printer& p) {
    os << p.res.pretty_print(p.s, p.slice);
    return os;
}

void result::ensure_counts() {
    if (!_partition_count || !row_count()) {
        uint64_t row_count;
        std::tie(_partition_count, row_count) = result_view::do_with(*this, [this] (auto&& view) {
            return view.count_partitions_and_rows();
        });
        set_row_count(row_count);
    }
}

result::result()
    : result([] {
        bytes_ostream out;
        ser::writer_of_query_result<bytes_ostream>(out).skip_partitions().skip_last_position().end_query_result();
        return out;
    }(), short_read::no, 0, 0)
{ }

static void write_partial_partition(ser::writer_of_qr_partition<bytes_ostream>&& pw, const ser::qr_partition_view& pv, uint64_t rows_to_include) {
    auto key = pv.key();
    auto static_cells_wr = (key ? std::move(pw).write_key(*key) : std::move(pw).skip_key())
            .start_static_row()
            .start_cells();
    for (auto&& cell : pv.static_row().cells()) {
        static_cells_wr.add(cell);
    }
    auto rows_wr = std::move(static_cells_wr)
            .end_cells()
            .end_static_row()
            .start_rows();
    auto rows = pv.rows();
    // rows.size() can be 0 is there's a single static row
    auto it = rows.begin();
    for (uint64_t i = 0; i < std::min(rows.size(), rows_to_include); ++i) {
        rows_wr.add(*it++);
    }
    std::move(rows_wr).end_rows().end_qr_partition();
}

foreign_ptr<lw_shared_ptr<query::result>> result_merger::get() {
    if (_partial.size() == 1) {
        return std::move(_partial[0]);
    }

    bytes_ostream w;
    auto partitions = ser::writer_of_query_result<bytes_ostream>(w).start_partitions();
    uint64_t row_count = 0;
    short_read is_short_read;
    uint32_t partition_count = 0;

    std::optional<full_position> last_position;

    for (auto&& r : _partial) {
        result_view::do_with(*r, [&] (result_view rv) {
            last_position.reset();
            for (auto&& pv : rv._v.partitions()) {
                auto rows = pv.rows();
                // If rows.empty(), then there's a static row, or there wouldn't be a partition
                const uint64_t rows_in_partition = rows.size() ? : 1;
                const uint64_t rows_to_include = std::min(_max_rows - row_count, rows_in_partition);
                row_count += rows_to_include;
                if (rows_to_include >= rows_in_partition) {
                    partitions.add(pv);
                    if (++partition_count >= _max_partitions) {
                        return;
                    }
                } else if (rows_to_include > 0) {
                    ++partition_count;
                    write_partial_partition(partitions.add(), pv, rows_to_include);
                    return;
                } else {
                    return;
                }
            }
            last_position = rv._v.last_position();
        });
        if (r->is_short_read()) {
            is_short_read = short_read::yes;
            last_position.reset();
            break;
        }
        if (row_count >= _max_rows || partition_count >= _max_partitions) {
            last_position.reset();
            break;
        }
    }

    auto after_partitions = std::move(partitions).end_partitions();
    if (last_position) {
        std::move(after_partitions).write_last_position(*last_position).end_query_result();
    } else {
        std::move(after_partitions).skip_last_position().end_query_result();
    }

    return make_foreign(make_lw_shared<query::result>(std::move(w), is_short_read, row_count, partition_count));
}

static bytes_opt merge_singular_results(bytes_opt r1, bytes_opt r2, forward_request::reduction_type reduction) {
    switch (reduction) {
        case forward_request::reduction_type::count: {
            auto count1 = value_cast<int64_t>(long_type->deserialize(bytes_view(*r1)));
            auto count2 = value_cast<int64_t>(long_type->deserialize(bytes_view(*r2)));
            return data_value(count1 + count2).serialize();
        }
    }
    throw std::runtime_error("unknown reduction type");
}

void forward_result::merge(const forward_result& other, const std::vector<forward_request::reduction_type>& reduction_types) {
    if (query_results.empty()) {
        query_results.resize(other.query_results.size());
    }

    if (query_results.size() != other.query_results.size() || query_results.size() != reduction_types.size()) {
        on_internal_error(
            qlogger,
            format("forward_result::merge(): operation cannot be completed due to invalid argument sizes. "
                    "this.query_results.size(): {} "
                    "other.query_results.size(): {} "
                    "reduction_types.size(): {}",
                    query_results.size(), other.query_results.size(), reduction_types.size())
        );
    }

    for (size_t i = 0; i < query_results.size(); i++) {
        query_results[i] = merge_singular_results(query_results[i], other.query_results[i], reduction_types[i]);
    }
}

std::ostream& operator<<(std::ostream& out, const query::forward_result::printer& p) {
    if (p.types.size() != p.res.query_results.size()) {
        return out << "[malformed forward_result (" << p.res.query_results.size()
            << " results, " << p.types.size() << " reduction types)]";
    }

    out << "[";
    for (size_t i = 0; i < p.types.size(); i++) {
        switch (p.types[i]) {
            case forward_request::reduction_type::count: {
                auto count = value_cast<int64_t>(long_type->deserialize(bytes_view(*p.res.query_results[i])));
                out << count;
            }
        }

        if (i + 1 < p.types.size()) {
            out << ", ";
        }
    }
    return out << "]";
}

}

std::optional<query::clustering_range> position_range_to_clustering_range(const position_range& r, const schema& s) {
    assert(r.start().get_type() == partition_region::clustered);
    assert(r.end().get_type() == partition_region::clustered);

    if (r.start().has_key() && r.end().has_key()
            && clustering_key_prefix::equality(s)(r.start().key(), r.end().key())) {
        assert(r.start().get_bound_weight() != r.end().get_bound_weight());

        if (r.end().get_bound_weight() == bound_weight::after_all_prefixed
                && r.start().get_bound_weight() != bound_weight::after_all_prefixed) {
            // [before x, after x) and [for x, after x) get converted to [x, x].
            return query::clustering_range::make_singular(r.start().key());
        }

        // [before x, for x) does not contain any keys.
        return std::nullopt;
    }

    // position_range -> clustering_range
    // (recall that position_ranges are always left-closed, right opened):
    // [before x, ...), [for x, ...) -> [x, ...
    // [after x, ...) -> (x, ...
    // [..., before x), [..., for x) -> ..., x)
    // [..., after x) -> ..., x]

    auto to_bound = [&s] (const position_in_partition& p, bool left) -> std::optional<query::clustering_range::bound> {
        if (p.is_before_all_clustered_rows(s)) {
            assert(left);
            return {};
        }

        if (p.is_after_all_clustered_rows(s)) {
            assert(!left);
            return {};
        }

        assert(p.has_key());

        auto bw = p.get_bound_weight();
        bool inclusive = left
            ? bw != bound_weight::after_all_prefixed
            : bw == bound_weight::after_all_prefixed;

        return query::clustering_range::bound{p.key(), inclusive};
    };

    return query::clustering_range{to_bound(r.start(), true), to_bound(r.end(), false)};
}
