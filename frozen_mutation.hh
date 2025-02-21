/*
 * Copyright (C) 2015-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#pragma once

#include "dht/i_partitioner.hh"
#include "replica/database_fwd.hh"
#include "mutation_fragment.hh"
#include "mutation_fragment_v2.hh"
#include "mutation_partition_view.hh"
#include "mutation_consumer_concepts.hh"
#include "range_tombstone_change_generator.hh"
#include "schema.hh"

class mutation;
class flat_mutation_reader_v2;

namespace ser {
class mutation_view;
}

template<typename Result>
struct frozen_mutation_consume_result {
    stop_iteration stop;
    Result result;
};

template<>
struct frozen_mutation_consume_result<void> {
    stop_iteration stop;
};

// mutation_partition_view visitor which consumes a frozen_mutation.
template<FlattenedConsumerV2 Consumer>
class frozen_mutation_consumer_adaptor final : public mutation_partition_view_virtual_visitor {
private:
    const schema& _schema;
    std::optional<dht::decorated_key> _dk;
    lazy_row _static_row;
    range_tombstone_change_generator _rt_gen;
    alloc_strategy_unique_ptr<rows_entry> _current_row_entry;
    deletable_row* _current_row = nullptr;
    Consumer& _consumer;
    stop_iteration _stop_consuming = stop_iteration::no;

    void flush_rows_and_tombstones(position_in_partition_view pos) {
        if (!_static_row.empty()) {
            auto row = std::move(_static_row.get_existing());
            if (!_stop_consuming) {
                _stop_consuming = _consumer.consume(static_row(std::move(row)));
            }
        }
        _rt_gen.flush(pos, [this] (range_tombstone_change rt) {
            if (!_stop_consuming) {
                _stop_consuming = _consumer.consume(std::move(rt));
            }
        });
        if (_current_row) {
            auto row_entry = std::move(_current_row_entry);
            _current_row = nullptr;
            if (!_stop_consuming) {
                _stop_consuming = _consumer.consume(clustering_row(std::move(*row_entry)));
            }
        }
    }

public:
    frozen_mutation_consumer_adaptor(schema_ptr s, Consumer& consumer)
        : _schema(*s)
        , _rt_gen(_schema)
        , _consumer(consumer)
    {
    }

    Consumer& consumer() {
        return _consumer;
    }

    void on_new_partition(const partition_key& key) {
        _rt_gen.reset();
        _dk = dht::decorate_key(_schema, key);
        _consumer.consume_new_partition(*_dk);
    }

    virtual void accept_partition_tombstone(tombstone t) override {
        _consumer.consume(t);
    }

    virtual void accept_static_cell(column_id id, atomic_cell cell) override {
        row& r = _static_row.maybe_create();
        r.append_cell(id, atomic_cell_or_collection(std::move(cell)));
    }

    virtual void accept_static_cell(column_id id, collection_mutation_view collection) override {
        row& r = _static_row.maybe_create();
        r.append_cell(id, collection_mutation(*_schema.static_column_at(id).type, std::move(collection)));
    }

    virtual void accept_row_tombstone(range_tombstone rt) override {
        flush_rows_and_tombstones(rt.position());
        _rt_gen.consume(std::move(rt));
    }

    virtual void accept_row(position_in_partition_view key, row_tombstone deleted_at, row_marker rm, is_dummy dummy, is_continuous continuous) override {
        flush_rows_and_tombstones(key);
        _current_row_entry = alloc_strategy_unique_ptr<rows_entry>(current_allocator().construct<rows_entry>(_schema, key, dummy, continuous));
        deletable_row& r = _current_row_entry->row();
        r.apply(rm);
        r.apply(deleted_at);
        _current_row = &r;
    }

    void accept_row_cell(column_id id, atomic_cell cell) override {
        row& r = _current_row->cells();
        r.append_cell(id, std::move(cell));
    }

    virtual void accept_row_cell(column_id id, collection_mutation_view collection) override {
        row& r = _current_row->cells();
        r.append_cell(id, collection_mutation(*_schema.regular_column_at(id).type, std::move(collection)));
    }

    auto on_end_of_partition() {
        flush_rows_and_tombstones(position_in_partition::after_all_clustered_rows());
        if (_consumer.consume_end_of_partition()) {
            _stop_consuming = stop_iteration::yes;
        }
        using consume_res_type = decltype(_consumer.consume_end_of_stream());
        if constexpr (std::is_same_v<consume_res_type, void>) {
            _consumer.consume_end_of_stream();
            return frozen_mutation_consume_result<void>{_stop_consuming};
        } else {
            return frozen_mutation_consume_result<consume_res_type>{_stop_consuming, _consumer.consume_end_of_stream()};
        }
    }
};

// Immutable, compact form of mutation.
//
// This form is primarily destined to be sent over the network channel.
// Regular mutation can't be deserialized because its complex data structures
// need schema reference at the time object is constructed. We can't lookup
// schema before we deserialize column family ID. Another problem is that even
// if we had the ID somehow, low level RPC layer doesn't know how to lookup
// the schema. Data can be wrapped in frozen_mutation without schema
// information, the schema is only needed to access some of the fields.
//
class frozen_mutation final {
private:
    bytes_ostream _bytes;
    partition_key _pk;
private:
    partition_key deserialize_key() const;
    ser::mutation_view mutation_view() const;
public:
    explicit frozen_mutation(const mutation& m);
    explicit frozen_mutation(bytes_ostream&& b);
    frozen_mutation(bytes_ostream&& b, partition_key key);
    frozen_mutation(frozen_mutation&& m) = default;
    frozen_mutation(const frozen_mutation& m) = default;
    frozen_mutation& operator=(frozen_mutation&&) = default;
    frozen_mutation& operator=(const frozen_mutation&) = default;
    const bytes_ostream& representation() const { return _bytes; }
    utils::UUID column_family_id() const;
    utils::UUID schema_version() const; // FIXME: Should replace column_family_id()
    partition_key_view key() const;
    dht::decorated_key decorated_key(const schema& s) const;
    mutation_partition_view partition() const;
    // The supplied schema must be of the same version as the schema of
    // the mutation which was used to create this instance.
    // throws schema_mismatch_error otherwise.
    mutation unfreeze(schema_ptr s) const;
    future<mutation> unfreeze_gently(schema_ptr s) const;

    // Automatically upgrades the stored mutation to the supplied schema with custom column mapping.
    mutation unfreeze_upgrading(schema_ptr schema, const column_mapping& cm) const;

    // Consumes the frozen mutation's content.
    //
    // The consume operation is stoppable:
    // * To stop, return stop_iteration::yes from one of the consume() methods;
    // * The consume will now stop and return;
    //
    // Note that `consume_end_of_partition()` and `consume_end_of_stream()`
    // will be called each time the consume is stopping, regardless of whether
    // you are pausing or the consumption is ending for good.
    template<FlattenedConsumerV2 Consumer>
    auto consume(schema_ptr s, Consumer& consumer) const -> frozen_mutation_consume_result<decltype(consumer.consume_end_of_stream())>;

    template<FlattenedConsumerV2 Consumer>
    auto consume(schema_ptr s, frozen_mutation_consumer_adaptor<Consumer>& adaptor) const -> frozen_mutation_consume_result<decltype(adaptor.consumer().consume_end_of_stream())>;

    // Consumes the frozen mutation's content.
    //
    // The consume operation is stoppable:
    // * To stop, return stop_iteration::yes from one of the consume() methods;
    // * The consume will now stop and return;
    //
    // Note that `consume_end_of_partition()` and `consume_end_of_stream()`
    // will be called each time the consume is stopping, regardless of whether
    // you are pausing or the consumption is ending for good.
    template<FlattenedConsumerV2 Consumer>
    auto consume_gently(schema_ptr s, Consumer& consumer) const -> future<frozen_mutation_consume_result<decltype(consumer.consume_end_of_stream())>>;

    template<FlattenedConsumerV2 Consumer>
    auto consume_gently(schema_ptr s, frozen_mutation_consumer_adaptor<Consumer>& adaptor) const -> future<frozen_mutation_consume_result<decltype(adaptor.consumer().consume_end_of_stream())>>;

    unsigned shard_of(const schema& s) const {
        return dht::shard_of(s, dht::get_token(s, key()));
    }

    struct printer {
        const frozen_mutation& self;
        schema_ptr schema;
        friend std::ostream& operator<<(std::ostream&, const printer&);
    };

    // Same requirements about the schema as unfreeze().
    printer pretty_printer(schema_ptr) const;
};

frozen_mutation freeze(const mutation& m);

struct frozen_mutation_and_schema {
    frozen_mutation fm;
    schema_ptr s;
};

// Can receive streamed_mutation in reversed order.
class streamed_mutation_freezer {
    const schema& _schema;
    partition_key _key;
    bool _reversed;

    tombstone _partition_tombstone;
    std::optional<static_row> _sr;
    std::deque<clustering_row> _crs;
    range_tombstone_list _rts;
public:
    streamed_mutation_freezer(const schema& s, const partition_key& key, bool reversed = false)
        : _schema(s), _key(key), _reversed(reversed), _rts(s) { }

    stop_iteration consume(tombstone pt);

    stop_iteration consume(static_row&& sr);
    stop_iteration consume(clustering_row&& cr);

    stop_iteration consume(range_tombstone&& rt);

    frozen_mutation consume_end_of_stream();
};

static constexpr size_t default_frozen_fragment_size = 128 * 1024;

using frozen_mutation_consumer_fn = std::function<future<stop_iteration>(frozen_mutation, bool)>;
future<> fragment_and_freeze(flat_mutation_reader_v2 mr, frozen_mutation_consumer_fn c,
                             size_t fragment_size = default_frozen_fragment_size);

class reader_permit;

class frozen_mutation_fragment {
    bytes_ostream _bytes;
public:
    explicit frozen_mutation_fragment(bytes_ostream bytes) : _bytes(std::move(bytes)) { }
    const bytes_ostream& representation() const { return _bytes; }

    mutation_fragment unfreeze(const schema& s, reader_permit permit);

    future<> clear_gently() noexcept {
        return _bytes.clear_gently();
    }
};

frozen_mutation_fragment freeze(const schema& s, const mutation_fragment& mf);

template<FlattenedConsumerV2 Consumer>
auto frozen_mutation::consume(schema_ptr s, frozen_mutation_consumer_adaptor<Consumer>& adaptor) const -> frozen_mutation_consume_result<decltype(adaptor.consumer().consume_end_of_stream())> {
    check_schema_version(schema_version(), *s);
    try {
        adaptor.on_new_partition(_pk);
        partition().accept(s->get_column_mapping(), adaptor);
        return adaptor.on_end_of_partition();
    } catch (...) {
        std::throw_with_nested(std::runtime_error(format(
                "frozen_mutation::consume(): failed consuming mutation {} of {}.{}", key(), s->ks_name(), s->cf_name())));
    }
}

template<FlattenedConsumerV2 Consumer>
auto frozen_mutation::consume(schema_ptr s, Consumer& consumer) const -> frozen_mutation_consume_result<decltype(consumer.consume_end_of_stream())> {
    frozen_mutation_consumer_adaptor adaptor(s, consumer);
    return consume(s, adaptor);
}

template<FlattenedConsumerV2 Consumer>
auto frozen_mutation::consume_gently(schema_ptr s, frozen_mutation_consumer_adaptor<Consumer>& adaptor) const -> future<frozen_mutation_consume_result<decltype(adaptor.consumer().consume_end_of_stream())>> {
    check_schema_version(schema_version(), *s);
    try {
        adaptor.on_new_partition(_pk);
        return partition().accept_gently(s->get_column_mapping(), adaptor).then_wrapped([this, s, &adaptor] (future<> f) {
            if (f.failed()) {
                std::throw_with_nested(std::runtime_error(format(
                        "frozen_mutation::consume_gently(): failed consuming mutation {} of {}.{}", key(), s->ks_name(), s->cf_name())));
            }
            return adaptor.on_end_of_partition();
        });
    } catch (...) {
        std::throw_with_nested(std::runtime_error(format(
                "frozen_mutation::consume_gently(): failed consuming mutation {} of {}.{}", key(), s->ks_name(), s->cf_name())));
    }
}

template<FlattenedConsumerV2 Consumer>
auto frozen_mutation::consume_gently(schema_ptr s, Consumer& consumer) const -> future<frozen_mutation_consume_result<decltype(consumer.consume_end_of_stream())>> {
    frozen_mutation_consumer_adaptor adaptor(s, consumer);
    co_return co_await consume_gently(s, adaptor);
}
