# 开局底图与实时差量

`run.py run` 默认从 `B03_DATA` 读取本规模的外部场景。增加 `--foundation` 后，OM 和 RC 各自获得 0 秒拓扑、开局业务、队列与控制预留。外部官方场景记录实际接口数。自建场景按其声明检查接线和资源上限。底图位于每次运行的 `network-foundation.json`，归档原始文件、有效输入和选择参数供复算。

## 自建场景与信道

以下两次运行使用相同的四节点合成场景。信道模型依据其版本参数分别折算链路容量、延迟和丢包。可以比较两个运行目录中的原始输入、有效输入和读回轨迹。

```sh
python run.py run --network field-survey --no-channel --mode package --start 0 --end 55 --drain 2 --output runs/plain
python run.py run --network field-survey --channel-profile survey-noise-v1 --mode package --start 0 --end 55 --drain 2 --output runs/noise
python run.py verify runs/plain
python run.py verify runs/noise
python run.py knowledge --channel-profile survey-noise-v1 --edition 1.0.0
```

自建场景也可通过 `--scenario <manifest.json>` 载入。场景文件提供者负责提供原始场景许可。仓内示例为手工合成数据，官方场景及派生场景的数据目录由使用者在仓外提供。[场景清单](../knowledge/scenarios/field-survey/network.json) · [信道版本](../knowledge/channels/survey-noise-v1.json)

## 单张工单

OM 用已送达且可用的链路报告计算相对开局降速的链路，用已登记的业务报告识别开局名册之外的新业务。RC 从同一工单快照独立复算，校验授权、差量和选中的知识条目。固定规则和原冻结模型继续使用原决策输入。选取部分开局业务时，`--services <开局名单> --include-arrivals` 允许后来注册的业务加入仿真负载。OM 和 RC 按报告抵达顺序发现这些业务，离线核验逐一检查首次可见时间。

新版中文技术知识包含五条诊断指引与两张案例卡。使用 `--components config/knowledge-om.json` 打开按诊断标签匹配、去重、打包和校验。模型和规则提供方须在自己的注册项中声明接受知识，才会读取中文上下文。默认配置沿用原知识版。诊断、授权与设备写入分别留有单独回执。[知识组接口](../collab/knowledge/README.md)

## 新策略选择器

注册 `model.options.acceptNetworkFoundation` 的模型实现 `set_network_foundation(catalog)`，在开局接收一次底图。每次 `choose` 接收 `networkReference`，其中包括目录摘要、降速链路与新增业务列表。示例选择器使用 B（优先级贪心）和 I（有界局部调整），由规则组件生成配置。训练通过已核验工单的未加权业务秒更新策略，加权指标 W 单独评价。

```sh
python collab/model/train_foundation.py init --output checkpoints/v0
python run.py run --data "$B03_DATA" --script 04 --foundation --policy model --components config/foundation-sample.json --checkpoint checkpoints/v0 --device cpu --output runs/draw-1
```

为同一工况提供至少两个独立采样种子。可在另一个 JSON 组件清单中继承 `config/foundation-sample.json`，把 `model.options.drawSeed` 设为另一个整数，用该清单对相同的脚本、窗口和业务再次执行上面的命令，输出至 `runs/draw-2`。组件清单中的 `extends` 按自身所在目录解析。每一批使用一个起始检查点，下一批从更新后的检查点重新采样。

```sh
python collab/model/train_foundation.py update --checkpoint checkpoints/v0 --output checkpoints/v1 --report runs/update-1.json runs/draw-1 runs/draw-2
python run.py run --data "$B03_DATA" --foundation --policy model --components config/foundation-selector.json --checkpoint checkpoints/v1 --device cpu --output runs/selector
python run.py verify runs/selector
```

[模型选择器](../src/foundation_policy.py) · [训练和回报核算](../src/foundation_training.py) · [模型组接口](../collab/model/README.md)
