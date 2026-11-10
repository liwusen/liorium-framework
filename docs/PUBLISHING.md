# 发布到 PyPI（Trusted Publishing）

Liorium 的 12 个包全部通过 **Trusted Publishing（OIDC）** 发布：GitHub Actions 用短期
OIDC token 向 PyPI 换取一次性上传凭证，**仓库和 GitHub 里都不存任何 PyPI token**。

工作流：[`.github/workflows/publish.yml`](../.github/workflows/publish.yml)

```
tag v0.1.0 ──▶ build（12 个包并行构建 + twine check）
            └▶ publish（12 个包逐个发布，各自用 OIDC 换 token）
               └▶ release（把 sdist/wheel 挂到 GitHub Release）
```

## 一、PyPI 侧一次性配置（必须由维护者手工做）

对**每一个包**（共 12 个）在 <https://pypi.org/manage/account/publishing/> 添加一条
*pending publisher*（项目还不存在时）或 *trusted publisher*（项目已存在时），字段完全一致：

| 字段 | 值 |
|---|---|
| PyPI Project Name | `liorium-core`、`liorium-vfs`、`liorium-realm-base`、`liorium-core-tool`、`liorium-core-tool-realm`、`liorium-memory`、`liorium-skills`、`liorium-mcp`、`liorium-todo-realm`、`liorium-provider-manager`、`liorium-agent`、`liorium-cli` |
| Owner | `liwusen` |
| Repository name | `liorium-framework` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

想先演练一遍就用 TestPyPI（<https://test.pypi.org/manage/account/publishing/>）配同样的字段、
environment 填 `testpypi`，然后手动触发 workflow 选 `testpypi`。

> 名字写错任何一个字符都会得到 `invalid-publisher` 错误，且**不会**退化成密码登录。
> `Environment name` 留空也可以，但要同步删掉 workflow 里的 `environment:`，两者必须一致。

## 二、发一个版本

```bash
# 1. 改版本号：12 个 packages/*/pyproject.toml 里的 version 必须一致
#    （scripts/check_version.py 会强制校验，不一致直接失败）
# 2. 提交并打 tag
git tag v0.1.0
git push origin main --tags
```

推送 `v*` tag 即触发发布。只想重跑某一个包时，用 Actions 页面 **Re-run failed jobs**；
同版本已存在于 PyPI 时 `skip-existing: true` 会跳过而不是报错。

手动触发（Actions → Publish to PyPI → Run workflow）可指定 `pypi` / `testpypi`。

## 三、本地预跑同样的步骤

```powershell
# 版本一致性
uv run --no-project python scripts/check_version.py v0.1.0 --all

# 构建全部 12 个包并检查元数据
uv build --all-packages --out-dir dist
uv run --no-project --with twine twine check --strict dist/*

# 用 TestPyPI 试发（需要自己账号的 token，仅本地演练用）
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-XXX dist/*
```

## 四、为什么不建 `release-package.yml`

旧式做法（`twine upload` + `PYPI_API_TOKEN` secret）把长期凭据放进仓库；Trusted Publishing
把凭据换成"这个 workflow、这个仓库、这个 environment 才能换到"的一次性 token，
泄露面从"永久 token"缩到"一次构建"，因此本仓库只保留 OIDC 这一条发布路径。

## 五、常见失败

| 报错 | 原因 |
|---|---|
| `invalid-publisher` / `OIDC token exchange failed` | PyPI 侧的 owner / repo / workflow / environment 与 workflow 不一致 |
| `File already exists` | 同版本已上传过；确认 `skip-existing` 生效，或改版本号 |
| `twine check` 失败 | `README.md` 的相对链接失效或缺少 `readme` 字段 |
| 某个包的依赖装不上 | 该包 `Requires-Dist` 指向的兄弟包版本尚未发布；先发叶子包（workflow 里已按依赖顺序排列） |
