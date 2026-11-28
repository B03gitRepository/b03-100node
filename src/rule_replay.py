"""基于封存观测与宿主契约，离线重建规则输入与最终设备决策。"""
from pathlib import Path
from types import SimpleNamespace
import sys


def host_roles():
    # 内置分组包已提供仅依赖标准库的 QoS 算术工具。
    # 导入角色辅助模块不会启动仿真引擎或 A2A 服务。
    from scene import ROOT
    packet = str(ROOT / "src" / "packet")
    bundled = str(ROOT / "simulator" / "resnet_py" / "src")
    for directory in (packet, bundled):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    import packet_roles
    return packet_roles


def replay_diagnosis(conditions, state):
    """使用归档授权与当前控制节拍前已送达的报告，离线重建 OM 诊断视图。"""
    roles = host_roles()
    grant = conditions["authority"]
    allowed = None if grant["discovery"] == "all_delivered_registered" else grant["allowedServiceIds"]
    waterline = conditions["simulation"]["control"]["control_headroom_fraction"]
    agents = roles._PacketAgents(allowed, grant["protectedServiceIds"], waterline,
                                 SimpleNamespace(identity=conditions["rcPolicy"]))
    allowed, protected = agents.scopes(state)
    snapshot, ledger = roles._model(state, waterline, allowed - protected)
    config = roles._configuration(protected, snapshot)
    # 旧版本运行虽然也送达了同样的探针，但当时尚不支持开启目的离线停源工单；
    # 此处按封存的产出方源码清单判定，离线回放绝不能事后追授新权限。
    allow_source_stop = "src/packet/destination_guard.py" in conditions.get("implementationSha256", {})
    diagnosis = roles._diagnose(state, snapshot, ledger, config, allowed, protected,
                                allow_source_stop=allow_source_stop)
    diagnosis["authority_mode"] = "all_arrived_registered" if agents.allowed is None else "explicit_whitelist"
    return roles, snapshot, config, ledger, diagnosis, agents.policy_hash


def from_observation(conditions, state, expected_diagnosis, plan, request, host_config):
    roles, snapshot, config, ledger, diagnosis, policy_hash = replay_diagnosis(conditions, state)
    fields = {k:expected_diagnosis.get(k) for k in diagnosis}
    held = roles._conditional_reservations(state, diagnosis["authority"])
    if (request.get("conditionalReservations", {}) != held or fields != diagnosis
            or request["authority"] != diagnosis["authority"] or request["snapshot"] != snapshot
            or host_config != config or plan["policy_hash"] != policy_hash
            or plan["authorized"] != diagnosis["authority"]["executableServiceIds"]
            or state["now_ns"] != plan["time_ns"]):
        raise ValueError("Rule input differs from the sealed OM observation, grant, host config or policy")
    return roles, snapshot, config, ledger


def verify_final(conditions, plan, state, request, roles, snapshot, config, ledger, target):
    from rule_patch import check_final
    from strong_tools import digest, validate_proposal
    audit = plan["solver_audit"]
    authority = request["authority"]
    action = request["action"]
    first = audit.get("openingAdmission")
    if first is not None:
        from opening import check_opening
        hold = action["tool"] == "hold_current" or action["scope"]["kind"] in ("retain","current")
        target, admission = check_opening(target, snapshot, config, authority,
                                          conditions["rcPolicy"]["initialAdmission"], hold, rule_request=request,
                                          rule_output=audit["rulePatch"]["output"])
        if first != admission:
            raise ValueError("Opening supplement did not follow the sealed action and output budgets")
    expected = {}
    rejected = []
    for sid in plan["authorized"]:
        demand = state["services"][sid]
        inactive = not (demand["active"] and demand["registered"])
        reason = "reported_inactive" if inactive else "no_feasible_b03_candidate"
        choice = roles._choice(demand["service"], target[sid], state["links"], revision=demand["revision"], reason=reason)
        expected[sid] = choice
        if target[sid] is not None and not choice["accepted"] and target[sid] != snapshot["assignments"][sid]:
            rejected.append(sid)
            target[sid] = None
    if expected != plan["decisions"] or rejected != audit.get("strictPacketQosRejected",[]):
        raise ValueError("Final per-service decisions or rejection reasons differ from the replay")
    if digest(target) != audit["targetHash"] or not validate_proposal(snapshot,config,authority,target)["valid"]:
        raise ValueError("Final rule allocation could not be reconstructed")
    check_final(request,target)
    bound = {"snapshot":snapshot,"authority":authority}
    # 公开区间核验器同样复用原始公共绑定信息。
    bound.update(config=config,identity=audit["binding"])
    logical = roles.candidate_scope(bound,bound,target)
    physical, release, install, projected = roles._device_plan(bound,state,target,ledger,audit)
    if (plan["solver_scope"] != logical or plan["scope"] != physical
            or plan["release_ids"] != release or plan["install_ids"] != install
            or plan["wire_ledger"] != dict(ledger, proposed_wire_bps=projected)):
        raise ValueError("Final rule patch differs from release or installation scope")
