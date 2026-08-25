# Changelog
心流插件所有 Notable 变更都将记录在此文件中。
格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

## [1.2.5]
### Added
- 核心需求变更：只要心流判断触发Bot回复意图，无论Bot最终是否输出有效内容，都会立即开启/刷新沉浸模式，不再依赖Bot实际有回复才激活

### Fixed
- 修复致命崩溃Bug：`event.set_extra`/`event.get_extra`方法不存在的问题，改用`setattr`/`getattr`动态绑定事件属性，避免插件触发心流时崩溃
- 修复配置兼容性Bug：`dynamic_activity_weights`配置项现在同时支持WebUI传入的字符串和列表格式，避免配置加载时报错
- 修复逻辑缺陷：Bot无实际回复时未正确激活/刷新沉浸模式的问题

### Changed
- 优化配置体验：将评判提示词（`judge_prompt`）的配置输入框从单行输入框改为多行文本框，方便编辑长提示词
