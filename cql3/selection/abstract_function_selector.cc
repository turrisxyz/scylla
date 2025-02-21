/*
 * Modified by ScyllaDB
 * Copyright (C) 2015-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: (AGPL-3.0-or-later and Apache-2.0)
 */

#include "abstract_function_selector.hh"
#include "aggregate_function_selector.hh"
#include "scalar_function_selector.hh"
#include "to_string.hh"
#include "cql3/selection/selector_factories.hh"
#include "cql3/functions/abstract_function.hh"

namespace cql3 {

namespace selection {

bool abstract_function_selector::requires_thread() const {
    return _requires_thread;
}

shared_ptr<selector::factory>
abstract_function_selector::new_factory(shared_ptr<functions::function> fun, shared_ptr<selector_factories> factories) {
    if (fun->is_aggregate()) {
        if (factories->does_aggregation()) {
            throw exceptions::invalid_request_exception("aggregate functions cannot be used as arguments of aggregate functions");
        }
    } else {
        if (factories->does_aggregation() && !factories->contains_only_aggregate_functions()) {
            throw exceptions::invalid_request_exception(format("the {} function arguments must be either all aggregates or all none aggregates",
                                                            fun->name()));
        }
    }

    struct fun_selector_factory : public factory {
        shared_ptr<functions::function> _fun;
        shared_ptr<selector_factories> _factories;

        fun_selector_factory(shared_ptr<functions::function> fun,
                             shared_ptr<selector_factories> factories)
                : _fun(std::move(fun)), _factories(std::move(factories)) {
        }

        virtual sstring column_name() const override {
            return _fun->column_name(_factories->get_column_names());
        }

        virtual data_type get_return_type() const override {
            return _fun->return_type();
        }

        virtual shared_ptr<selector> new_instance() const override {
            using ret_type = shared_ptr<selector>;
            return _fun->is_aggregate() ? ret_type(::make_shared<aggregate_function_selector>(_fun, _factories->new_instances()))
                                        : ret_type(::make_shared<scalar_function_selector>(_fun, _factories->new_instances()));
        }

        virtual bool is_write_time_selector_factory() const override {
            return _factories->contains_write_time_selector_factory();
        }

        virtual bool is_ttl_selector_factory() const override {
            return _factories->contains_ttl_selector_factory();
        }

        virtual bool is_aggregate_selector_factory() const override {
            return _fun->is_aggregate() || _factories->contains_only_aggregate_functions();
        }

        virtual bool is_count_selector_factory() const override {
            auto p = dynamic_cast<functions::abstract_function*>(_fun.get());
            if (!p) {
                return false;
            }
            return p->name().name == "countRows";
        }
    };

    return make_shared<fun_selector_factory>(std::move(fun), std::move(factories));
}

bool scalar_function_selector::requires_thread() const {
    return fun()->requires_thread();
}

}

}
