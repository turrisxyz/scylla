/*
 * Copyright (C) 2020-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
#pragma once

#include <seastar/core/gate.hh>
#include "raft/raft.hh"
#include "message/messaging_service_fwd.hh"
#include "utils/UUID.hh"
#include "service/raft/raft_address_map.hh"
#include "service/raft/raft_state_machine.hh"

namespace service {

// Scylla-specific implementation of raft RPC module.
//
// Uses `netw::messaging_service` as an underlying implementation for
// actually sending RPC messages.
class raft_rpc : public raft::rpc {
    raft_state_machine& _sm;
    raft::group_id _group_id;
    raft::server_id _server_id;
    netw::messaging_service& _messaging;
    raft_address_map<>& _address_map;
    noncopyable_function<void(gms::inet_address, raft::server_id, bool added)> _on_server_update;
    seastar::gate _shutdown_gate;

    raft_ticker_type::time_point timeout() {
        return raft_ticker_type::clock::now() + raft_tick_interval * (raft::ELECTION_TIMEOUT.count() / 2);
    }

public:
    explicit raft_rpc(raft_state_machine& sm, netw::messaging_service& ms,
            raft_address_map<>& address_map, raft::group_id gid, raft::server_id srv_id,
            // Called when a server is added or removed from the RPC configuration.
            noncopyable_function<void(gms::inet_address, raft::server_id, bool added)> on_server_update);

    future<raft::snapshot_reply> send_snapshot(raft::server_id server_id, const raft::install_snapshot& snap, seastar::abort_source& as) override;
    future<> send_append_entries(raft::server_id id, const raft::append_request& append_request) override;
    void send_append_entries_reply(raft::server_id id, const raft::append_reply& reply) override;
    void send_vote_request(raft::server_id id, const raft::vote_request& vote_request) override;
    void send_vote_reply(raft::server_id id, const raft::vote_reply& vote_reply) override;
    void send_timeout_now(raft::server_id id, const raft::timeout_now& timeout_now) override;
    void send_read_quorum(raft::server_id id, const raft::read_quorum& check_quorum) override;
    void send_read_quorum_reply(raft::server_id id, const raft::read_quorum_reply& check_quorum_reply) override;
    future<raft::read_barrier_reply> execute_read_barrier_on_leader(raft::server_id id) override;
    future<raft::add_entry_reply> send_add_entry(raft::server_id id, const raft::command& cmd) override;
    future<raft::add_entry_reply> send_modify_config(raft::server_id id,
        const std::vector<raft::server_address>& add,
        const std::vector<raft::server_id>& del) override;

    void add_server(raft::server_id id, raft::server_info info) override;
    void remove_server(raft::server_id id) override;
    future<> abort() override;

    // Dispatchers to the `rpc_server` upon receiving an rpc message
    void append_entries(raft::server_id from, raft::append_request append_request);
    void append_entries_reply(raft::server_id from, raft::append_reply reply);
    void request_vote(raft::server_id from, raft::vote_request vote_request);
    void request_vote_reply(raft::server_id from, raft::vote_reply vote_reply);
    void timeout_now_request(raft::server_id from, raft::timeout_now timeout_now);
    void read_quorum_request(raft::server_id from, raft::read_quorum check_quorum);
    void read_quorum_reply(raft::server_id from, raft::read_quorum_reply check_quorum_reply);
    future<raft::read_barrier_reply> execute_read_barrier(raft::server_id);

    future<raft::snapshot_reply> apply_snapshot(raft::server_id from, raft::install_snapshot snp);
    future<raft::add_entry_reply> execute_add_entry(raft::server_id from, raft::command cmd);
    future<raft::add_entry_reply> execute_modify_config(raft::server_id from,
        std::vector<raft::server_address> add,
        std::vector<raft::server_id> del);
};

} // end of namespace service
