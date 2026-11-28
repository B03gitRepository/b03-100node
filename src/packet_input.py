"""针对超 token 上限的完整分组观测做无损区间压缩编码，不删减任何已送达证据。"""
from __future__ import annotations

from hashlib import sha256
from itertools import groupby

from frozen_policy import SCHEMA, canonical


ENCODING = "b03.packet.node-scope-ranges.v1"
_MARKER = "_ranges"
_INSTRUCTIONS = ("In nodes.rows IDs and in authorizedServiceSets, [lo,hi] means every integer "
                 "from lo through hi inclusive in order. Repeat a node row for each ID in its range.")


def _sha(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def _id(value):
    if type(value) is not int or value < 0:
        raise ValueError("A packet table or scope contains an invalid numeric ID")
    return value


def _runs(ids):
    """保持原始顺序、重复项与间隔；仅对长度不少于 3 的连续编号段做区间压缩。"""
    encoded = []
    for _, group in groupby(enumerate(map(_id, ids)), key=lambda item: item[1] - item[0]):
        run = [item[1] for item in group]
        if len(run) >= 3:
            encoded.append([run[0], run[-1]])
        else:
            encoded.extend(run)
    return encoded


def _expand(ids):
    for item in ids:
        if isinstance(item, list):
            if len(item) != 2 or _id(item[1]) - _id(item[0]) < 2:
                raise ValueError("Invalid inclusive packet ID range")
            yield from range(item[0], item[1] + 1)
        else:
            yield _id(item)


def encode_observation(raw):
    """仅压缩零掩码默认节点行以及各授权业务集内的连续编号段。"""
    if raw.get("schema") != SCHEMA or _MARKER in raw:
        raise ValueError("Expected an original released packet observation")
    node_rows = []
    for default, group in groupby(raw["nodes"]["rows"], key=lambda row: len(row) == 2 and row[1] == 0):
        if default:
            node_rows.extend([ident, 0] for ident in _runs(row[0] for row in group))
        else:
            node_rows.extend(group)
    return {**raw, _MARKER: _INSTRUCTIONS, "nodes": dict(raw["nodes"], rows=node_rows),
            "authorizedServiceSets": [_runs(scope) for scope in raw["authorizedServiceSets"]]}


def decode_observation(delivered):
    """完整还原原始 JSON 观测，包括所有例外行及作用域顺序。"""
    if _MARKER not in delivered:
        return delivered
    if delivered[_MARKER] != _INSTRUCTIONS:
        raise ValueError("Unknown packet range format")
    raw = dict(delivered)
    raw.pop(_MARKER)
    rows = []
    for row in raw["nodes"]["rows"]:
        if isinstance(row[0], list):
            if row[1:] != [0]:
                raise ValueError("Only a zero-mask default node row may contain an ID range")
            rows.extend([i, 0] for i in _expand([row[0]]))
        else:
            rows.append(row)
    raw["nodes"] = dict(raw["nodes"], rows=rows)
    raw["authorizedServiceSets"] = [list(_expand(scope)) for scope in raw["authorizedServiceSets"]]
    return raw


class InputTooLong(ValueError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(f"Complete packet input has {receipt['originalTokens']} original tokens and "
                         f"{receipt['deliveredTokens']} lossless tokens; model accepts {receipt['tokenLimit']}.")


def fit_complete_input(actor, observation, raw_ids, original_messages):
    """通过可逆闭区间编码把超限观测压缩进预填充窗口；拒绝不完整的截断观测。"""
    encoded = encode_observation(observation)
    if canonical(decode_observation(encoded)) != canonical(observation):
        raise ValueError("Packet input range round-trip changed the original observation")
    messages = [*original_messages[:-1], {"role": "user", "content": canonical(encoded)}]
    ids = actor.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    receipt = {
        "encoding": ENCODING, "originalTokens": len(raw_ids), "deliveredTokens": len(ids),
        "tokenLimit": actor.limit, "rawInputSha256": _sha(original_messages),
        "actualInputSha256": _sha(messages), "rawObservationSha256": _sha(observation),
        "actualObservationSha256": _sha(encoded), "rawTokenIdsSha256": _sha(raw_ids),
        "actualTokenIdsSha256": _sha(ids), "losslessVerified": True,
    }
    if not 0 < len(ids) <= actor.limit:
        raise InputTooLong(receipt)
    return ids, messages, receipt
