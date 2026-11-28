"""使用合成知识版离线自测知识检索组件（仅依赖 Python 标准库）。"""
import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE.parents[1]/"src"/"collab_ranked_knowledge.py"


# 自测流程：加载知识插件 -> 核对精确 ID 查询 -> 核对主题召回排序与去重 -> 输出规范化结果。
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider",type=Path,default=DEFAULT)
    ap.add_argument("--factory",default="build_knowledge")
    ap.add_argument("--case",type=Path,default=HERE/"case.json")
    args = ap.parse_args()
    case = json.loads(args.case.read_bytes())
    edition = case["edition"]
    spec = importlib.util.spec_from_file_location("team_knowledge",args.provider)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    lookup = getattr(plugin,args.factory)(config=deepcopy(case["config"]),library=deepcopy(edition)).lookup
    entries = {e["id"]:{**e,"version":edition["version"]} for e in edition["entries"]}
    for identifier in [*entries,"example-entry-that-does-not-exist"]:
        expected = [identifier] if identifier in entries else []
        if lookup(identifier=identifier,topic=None)!=expected:
            raise ValueError("Exact ID lookup disagrees with this edition")
    ranked = lookup(identifier=None,topic=case["topic"])
    if (not isinstance(ranked,list) or not all(isinstance(k,str) and k in entries for k in ranked)
            or len(set(ranked))!=len(ranked)):
        raise ValueError("Topic lookup requires a unique list of IDs from this edition")
    rows = [entries[k] for k in ranked]
    if (args.provider.resolve()==DEFAULT.resolve() and args.case.resolve()==HERE/"case.json"
            and (ranked!=case["expectedIDs"] or rows!=case["canonicalOutput"])):
        raise ValueError("Reference retrieval changed")
    print(json.dumps({"topic":case["topic"],"rankedIds":ranked,"canonicalOutput":rows},sort_keys=True))


if __name__=="__main__":
    main()
