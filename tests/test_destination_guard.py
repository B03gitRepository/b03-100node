"""Scope a stop to the source epoch that can still send into a disconnected destination."""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/packet"))
from destination_guard import choose_source_stop


def probe(stamp, peer_online, *, up=True):
    return {"sampled_at_ns": stamp, "node_online": True, "endpoint_online": peer_online,
            "admin_up": up, "status": "fresh"}


def episode():
    state = {"services": {}, "confirmed": {}, "links": {
        "last": {"src_node_id": "edge", "dst_node_id": "dest",
                 "reports": {"edge": probe(100, False), "dest": None}},
        "other": {"src_node_id": "backup", "dst_node_id": "dest",
                  "reports": {"backup": None, "dest": None}}}}
    for sid, rate in (("small", 3000), ("large", 5000)):
        state["services"][sid] = {"node_id": "source", "observed_at_ns": 96, "fresh": True,
                                  "registered": True, "active": True, "revision": 3,
                                  "service": {"src_node_id": "source", "dst_node_id": "dest"}}
        state["confirmed"][sid] = {"epoch": 7, "decision": {"node_path": ["source", "edge", "dest"],
                                                            "wire_bps": rate, "demand_revision": 3}}
    return state


class DestinationStopTests(unittest.TestCase):
    def test_largest_reachable_source_first_and_respects_authority(self):
        state=episode()
        stop=choose_source_stop(state, {"small","large"}, [])
        self.assertEqual((stop["service_id"],stop["epoch"],stop["link_id"]),("large",7,"last"))
        self.assertEqual(choose_source_stop(state, {"small"}, [])["service_id"], "small")
        self.assertIsNone(choose_source_stop(state, {"large"}, ["large"]))

    def test_newer_or_simultaneous_liveness_overrides_outage(self):
        for reporter, up in (("backup",True),("dest",False)):
            with self.subTest(reporter=reporter):
                state=episode()
                state["links"]["other"]["reports"][reporter]=probe(100,up)
                self.assertIsNone(choose_source_stop(state, {"small","large"}, []))

    def test_admin_down_stale_or_changed_demand_cannot_authorize_stop(self):
        for mut in ("admin", "stale_port", "revision", "stale_source", "active"):
            with self.subTest(mut=mut):
                state=episode()
                if mut=="admin": state["links"]["last"]["reports"]["edge"]["admin_up"]=False
                elif mut=="stale_port": state["links"]["last"]["reports"]["edge"]["status"]="stale"
                elif mut=="revision": state["services"]["large"]["revision"]+=1
                elif mut=="stale_source": state["services"]["large"]["fresh"]=False
                else: state["services"]["large"]["active"]=False
                self.assertIsNone(choose_source_stop(state, {"large"}, []))
