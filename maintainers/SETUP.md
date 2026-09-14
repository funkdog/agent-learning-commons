# 在 GitHub 上启用这一版

[返回首页](../README.md) · [维护指南](README.md)

这份内容包使用 GitHub 原生 Markdown、Discussions 与 PR。仓库名和所属账号尚未绑定；所有正文导航使用仓库内相对链接。

## 1. 放入目标仓库

将本目录的文件提交到目标仓库默认分支。注意包含隐藏的 `.github/` 目录，其中是讨论表单和 PR 模板。根目录 `README.md` 会成为仓库阅读入口。

如果目标仓库已有内容，先通过 PR 审阅合并，不覆盖现有文件。新仓库可以使用这份内容包初始化。

## 2. 启用 Discussions

仓库 Settings → General → Features → Discussions。只有提交表单文件不会自动开启讨论区。[GitHub 官方说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/enabling-or-disabling-github-discussions-for-a-repository)

在 Discussions 中创建以下分类。为让模板准确匹配，分类名称先使用表中的英文名，中文写在说明中；创建后检查分类 slug 与文件名一致。

| 名称 | 格式 | 中文说明 | 对应表单文件 |
| --- | --- | --- | --- |
| Explorations | Open-ended discussion | 探索笔记：分享阅读、实践与尚未完成的思考 | `.github/DISCUSSION_TEMPLATE/explorations.yml` |
| Questions | Question and answer | 提问求助：说清已尝试什么、卡在哪里 | `.github/DISCUSSION_TEMPLATE/questions.yml` |
| Experiments | Open-ended discussion | 实验与复现：分享实际执行的过程、结果和反例 | `.github/DISCUSSION_TEMPLATE/experiments.yml` |

现有默认分类可以保留；没有内容时再按需要精简。请勿为整理首版而删除已有社区讨论。

表单文件必须位于默认分支，其文件名对应分类 slug。[分类管理](https://docs.github.com/en/discussions/managing-discussions-for-your-community/managing-categories-for-discussions) · [表单匹配规则](https://docs.github.com/en/discussions/managing-discussions-for-your-community/creating-discussion-category-forms)

## 3. 建立少量主题标签

建议创建四个标签：`记忆与上下文`、`评测与实验`、`多 Agent 协作`、`阅读与思考`。颜色可自行选择，不依赖颜色表达含义。

表单中的“主题”只是正文信息，GitHub 不会因为选择某个下拉选项就自动添加标签。首版由有权限的维护者补标签；没有配置自动贴标服务。

在 Discussions 内可以用 `label:"记忆与上下文"` 搜索；也可以按分类、作者和更新时间查找。[搜索说明](https://docs.github.com/en/search-github/searching-on-github/searching-discussions)

## 4. 确认公开和投稿规则

由仓库所有者确认公开可见性、维护者权限和内容许可证。首版没有预先替所有贡献者设定许可证；确定后补充根目录许可文件，并相应更新贡献指南。

在公开征集投稿前说明转载条件、署名方式和贡献者认可方式。不要把许可未定的内容宣传为可任意复用。

## 5. 实际走一次验收

- 打开 README，按“第一次来 → 记忆专题 → 第一篇文章 → 下一篇”走一遍。
- 从文章返回专题或完整目录，确认链接在 GitHub 页面中可用。
- 打开 New discussion，分别选择三个分类，确认中文表单显示。
- 检查 Experiments 要求填写实际执行记录；计划应进入 Explorations 或 Questions。
- 在一次真实、有意发布的投稿后，检查正文、署名和主题信息是否保留。不要为测试而制造假实验。
- 提交一次实际需要的文档 PR，检查 PR 模板是否出现。

完成这些步骤后，再邀请第一批朋友参与。仓库文件准备完成不等于远端设置或表单已经验收。
