"""用于受控对照实验的独立模型适配器示例，直接复用锁定的参考权重。"""

from frozen_policy import FrozenActor


class ReferenceFrozenActor(FrozenActor):
    def choose(self, observation):
        selected, details = super().choose(observation)
        return selected, dict(details, policyVersion=self.version)


def build_model(*, checkpoint, tokenizer, device, config):
    if config["mode"] != "frozen" or not checkpoint:
        raise ValueError("The reference frozen adapter requires frozen mode and a pinned checkpoint")
    return ReferenceFrozenActor(checkpoint, tokenizer, device)
