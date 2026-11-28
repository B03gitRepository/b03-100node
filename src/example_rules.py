"""工具 I 改进示例：优先保障可选路径稀缺的业务，并利用剩余容量引导有界成对搜索。"""
from copy import deepcopy

import strong_tools
from b03.solver import loads
from rule_component import bind_proposal


class ResidualFirstRules:
    def __init__(self, config):
        self.aliases = dict(config.get("options", {}).get("toolAliases", {}))

    def enumerate_actions(self, snapshot, authority, config, history=None):
        native = strong_tools.enumerate_actions(snapshot, authority, config, history)
        by_id = {a["id"]: a for a in native}
        return native + [{**deepcopy(by_id[template]), "id": symbol} for symbol, template in self.aliases.items()
                         if template in by_id]

    def solve_action(self, action, bound, context=None):
        native = {**action, "id": self.aliases.get(action["id"], action["id"])}
        if native["id"] != "I":
            target, audit = strong_tools.solve_action(native, bound, context)
        else:
            target, audit = self._residual_pairs(native, bound)
        return bind_proposal(action, bound, target, {**audit, "action": deepcopy(action)})

    def _residual_pairs(self, action, bound):
        s, authority, config = bound["snapshot"], bound["authority"], bound["config"]
        if action not in strong_tools.enumerate_actions(s, authority, config):
            raise ValueError("I is not authorized on this snapshot")
        active, base, work, caps = strong_tools._setup(s, authority)
        budget = dict(strong_tools.BUDGETS[action["budget"]])
        budget["max_changed_services"] = config.get("t096_max_changed_services", len(authority["executableServiceIds"]))
        pools, search = strong_tools.generate_pool(s, work, base, config, action["pool"], budget)
        occupancy = loads(s["links"], base)

        # 在相同原始优先级下，优先照顾可行候选路径最少的业务。
        order = sorted(work, key=lambda sid: (
            -active[sid]["priority"], sum(strong_tools._fits(p, occupancy, caps) for p in pools[sid]), sid))
        for sid in order:
            def rank(path):
                demand = path["allocatedBps"]
                use = max(((occupancy[lid] + demand) / caps[lid] for lid in path["linkIds"]), default=0)
                return (not strong_tools._fits(path, occupancy, caps), use, tuple(path["linkIds"]))

            paths = pools[sid]
            paths.sort(key=rank)
            if paths and strong_tools._fits(paths[0], occupancy, caps):
                strong_tools._add(paths[0], occupancy, 1)

        # 复用工具 I 原有的有界双业务/单业务邻域结构及搜索步数上限。
        target, detail = strong_tools._local(s, active, base, order, pools, caps, budget)
        if sum(target[sid] != old for sid, old in s["assignments"].items()) > budget["max_changed_services"]:
            target = deepcopy(s["assignments"])
            detail["solverStatus"] = "change_budget_hold"
        return target, {**detail, "allocator": "residual-first bounded pair search",
                        "serviceOrder": order, "pathSearch": search, "budgetLimits": dict(strong_tools.BUDGETS[action["budget"]]),
                        "maxChangedServices": budget["max_changed_services"], "globallyOptimal": False}


def build_rules(*, config):
    return ResidualFirstRules(config)
