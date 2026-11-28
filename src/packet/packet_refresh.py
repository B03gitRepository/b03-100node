"""Independent, read-only configuration rounds on the existing native query channel."""


class IndependentRefresh:
    def __init__(self, loop):
        self.loop = loop
        self.jobs = {}
        self.retry_at = {}

    def start(self, now, work_id, targets):
        loop = self.loop
        for sid, target in targets.items():
            self.retry_at.pop(sid, None)
            deadline = now + loop.config["transaction_timeout_ns"]
            batch = {"work_id": work_id, "phase": "config_refresh", "joint_refresh": False,
                     "targets": {sid: target}, "received": {}, "waiting": {}, "deadline_ns": deadline}
            self.jobs[sid] = batch
            nodes = target["decision"]["node_path"]
            loop._event("readback_requested", now, work_id=work_id, phase="config_refresh", plan_id=target["plan_id"],
                        service_id=sid, epoch=target["epoch"], nodes=nodes, joint_refresh=False,
                        independent=True, deadline_ns=deadline, retained_wire_bps=loop.budgets())
            for node in nodes:
                loop._schedule_query(now, (sid, node), batch)

    def receive(self, ack, now, node):
        sample = ack.get("b03_readback")
        if not isinstance(sample, dict) or not isinstance(sample.get("service_id"), str):
            return False
        sid = sample["service_id"]
        batch = self.jobs.get(sid)
        item = batch and batch["waiting"].get((sid, node))
        if not item or not item["command"] or ack.get("command_id") != item["command"]["command_id"]:
            return False
        if now <= batch["deadline_ns"] and self.loop._accept_readout(batch, ack, now, node) and not batch["waiting"]:
            self._settle(sid, now)
        return True

    def poll(self, now):
        loop = self.loop
        links = None
        for sid, (deadline, since, missing) in list(self.retry_at.items()):
            if sid not in loop.confirmed or sid not in loop.refresh_blocks:
                del self.retry_at[sid]
                continue
            witnesses = {}
            if now < deadline:
                if not missing:
                    continue
                if links is None:
                    links = loop.link_reports.snapshot(now)
                witnesses = {node: loop._port_witness(node, since, links, now, online=True) for node in missing}
                if not all(witnesses.values()):
                    continue
            del self.retry_at[sid]
            loop.refresh_blocks.pop(sid, None)
            loop._event("config_refresh_retry_ready", now, service_id=sid, epoch=loop.confirmed[sid]["epoch"],
                        failed_at_ns=since, reason="reachable" if witnesses else "retry_deadline", witnesses=witnesses)
            loop.force_review = True
        for sid, batch in list(self.jobs.items()):
            if now > batch["deadline_ns"]:
                self._settle(sid, now, expired=True)
            else:
                loop._poll_query_waiters(batch, now)

    def cancel(self, sid, now, reason):
        batch = self.jobs.pop(sid, None)
        self.retry_at.pop(sid, None)
        if batch:
            self._result(sid, now, batch, "superseded", reason)

    def _settle(self, sid, now, *, expired=False):
        loop = self.loop
        batch = self.jobs[sid]
        target = batch["targets"][sid]
        record = loop.confirmed.get(sid)
        if not record or record["epoch"] != target["epoch"] or record["decision"] != target["decision"]:
            self.cancel(sid, now, "epoch_or_path_changed")
            return
        links, demands = loop._snapshots(now)
        ttl = loop.config["observation_ttl_ns"]
        if not expired:
            stale = [key for key, row in batch["received"].items()
                     if now - row["observed_at_ns"] > ttl or row["version"] < loop.versions.get(key[1], 0)]
            if stale and now < batch["deadline_ns"]:
                for key in stale:
                    del batch["received"][key]
                    loop._schedule_query(now, key, batch)
                return
        rows = {node: row for (_, node), row in batch["received"].items()}
        matched = loop._apply_refresh(sid, target, rows, links, demands, now)
        del self.jobs[sid]
        if not matched:
            missing = sorted(set(target["decision"]["node_path"]) - set(rows))
            self.retry_at[sid] = (now + loop.config["transaction_timeout_ns"], now, missing)
        self._result(sid, now, batch, "matched" if matched else "unconfirmed", "deadline" if expired else "all_replied")
        loop.force_review = True

    def _result(self, sid, now, batch, status, reason):
        loop = self.loop
        target = batch["targets"][sid]
        loop._event("config_refresh_service_result", now, work_id=batch["work_id"], plan_id=target["plan_id"],
                    service_id=sid, epoch=target["epoch"], status=status, reason=reason,
                    deadline_ns=batch["deadline_ns"], replan_before_write=True,
                    query_ids={node: row["query_id"] for (_, node), row in batch["received"].items()},
                    retained_wire_bps=loop.budgets())
