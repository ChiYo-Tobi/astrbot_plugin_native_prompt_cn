from astrbot.api.star import Context, Star

from .patcher import Patches


class NativePromptCN(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.patches = Patches(config if config is not None else {}, self.logger)

    async def initialize(self):
        try:
            self.patches.install()
        except Exception as exc:
            self.logger.warning('原生提示词定制未启用，保留 AstrBot 原文：%s', exc)
            return
        if self.patches.saved:
            self.logger.info('原生提示词定制已挂载：%d 个组装函数，%d 项文案', len(self.patches.saved), len(self.patches.active_keys))
        else:
            self.logger.warning('原生提示词定制未找到兼容组装点，全部保留原文')
        for name, reason in self.patches.skipped.items():
            self.logger.warning('原生提示词定制跳过 %s：%s（保留原文）', name, reason)

    async def terminate(self):
        self.patches.uninstall()
