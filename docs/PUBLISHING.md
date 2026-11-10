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

> **一个包一个 environment。** PyPI 对 GitHub publisher 的唯一性约束是
> `(owner, repo, workflow, environment)` 这个四元组的**全局唯一**——一个四元组只能登记
> 一个项目，pending publisher 和已存在项目的 publisher 共用这条约束。Liorium 的 12 个包
> 在同一个仓库、同一个 `publish.yml` 里发布，只能靠 **environment 名字**区分；如果 12 条
> 都填同一个 `pypi`，第 2 条就会报
> `A pending trusted publisher matching this configuration has already been registered for a different project name.`
> 官方说明见 [pypi/warehouse#16920](https://github.com/pypi/warehouse/issues/16920)。

在 <https://pypi.org/manage/account/publishing/> 用 **Add a new pending publisher → GitHub**
表单，为每个包各加一条，字段如下：

| 字段 | 值 |
|---|---|
| PyPI Project Name | 下表第 2 列 |
| Owner | `liwusen` |
| Repository name | `liorium-framework` |
| Workflow name | `publish.yml` |
| Environment name | 下表第 3 列（**逐行不同，不能都写 `pypi`**） |

| # | PyPI Project Name | Environment name | 已配好 |
|---|---|---|---|
| 1 | `liorium-core` | `pypi-liorium-core` | [ ] |
| 2 | `liorium-vfs` | `pypi-liorium-vfs` | [ ] |
| 3 | `liorium-realm-base` | `pypi-liorium-realm-base` | [ ] |
| 4 | `liorium-core-tool` | `pypi-liorium-core-tool` | [ ] |
| 5 | `liorium-core-tool-realm` | `pypi-liorium-core-tool-realm` | [ ] |
| 6 | `liorium-memory` | `pypi-liorium-memory` | [ ] |
| 7 | `liorium-skills` | `pypi-liorium-skills` | [ ] |
| 8 | `liorium-mcp` | `pypi-liorium-mcp` | [ ] |
| 9 | `liorium-todo-realm` | `pypi-liorium-todo-realm` | [ ] |
| 10 | `liorium-provider-manager` | `pypi-liorium-provider-manager` | [ ] |
| 11 | `liorium-agent` | `pypi-liorium-agent` | [ ] |
| 12 | `liorium-cli` | `pypi-liorium-cli` | [ ] |

environment 名字的规则就是 workflow 里的
`<target>-<package>`（`target` 是 `pypi` 或 `testpypi`），所以照抄第 3 列即可，
不用管 GitHub 那边：**这 12 个 environment 会在第一次发布时由 Actions 自动创建**，
不需要提前手工建。想加人工审批门禁，可以在 Settings → Environments 里给对应的
`pypi-*` 加 Required reviewers。

缺哪个包，那次发布就只有那个 job 报 `invalid-publisher`，其它包照发——所以可以配一条就发
一条，也可以 12 条全配齐再发。

依赖闭包参考：`pip install "liorium-agent[core]"` 需要前 6 个；`pip install liorium-cli`
需要前 11 个；`liorium-agent[mcp]` 才用到 `liorium-mcp`。**全部配齐**才能覆盖 CLI + mcp。

想先演练一遍就用 TestPyPI（<https://test.pypi.org/manage/account/publishing/>）配同样的
项目名，environment 填 `testpypi-<项目名>`（如 `testpypi-liorium-core`），然后手动触发
workflow 选 `testpypi`。TestPyPI 是独立的一套 publisher，与 PyPI 互不影响，可以不配。

想在配 PyPI **之前**先确认 12 个包都能构建：手动触发 workflow 选 `build-only`
（只跑 build 矩阵：`uv build --package` + `twine check --strict`，不发任何东西）。

> 名字写错任何一个字符都会得到 `invalid-publisher` 错误，且**不会**退化成密码登录。
> `Environment name` 留空也可以，但要同步把 workflow 里的 `environment:` 整段删掉；
> 而且留空后 12 个包的四元组都是 `(liwusen, liorium-framework, publish.yml, 空)`，
> 照样只能登记一个项目——所以**这里必须逐包填不同的名字**。

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

手动触发（Actions → Publish to PyPI → Run workflow）可指定 `pypi` / `testpypi` / `build-only`。
推荐顺序：先 `build-only` 跑绿 → 配好 12 条 publisher → 推 `v*` tag 真发。

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
| `A pending trusted publisher matching this configuration has already been registered for a different project name.` | 两个项目用了同一个 `(owner, repo, workflow, environment)`。本仓库必须一包一个 environment（`pypi-<项目名>`），见第一节 |
| `invalid-publisher` / `OIDC token exchange failed` | PyPI 侧的 owner / repo / workflow / environment 与 workflow 不一致（逐字符比对，含大小写） |
| `File already exists` | 同版本已上传过；确认 `skip-existing` 生效，或改版本号 |
| `twine check` 失败 | `README.md` 的相对链接失效或缺少 `readme` 字段 |
| 某个包的依赖装不上 | 该包 `Requires-Dist` 指向的兄弟包版本尚未发布；先发叶子包（workflow 里已按依赖顺序排列） |
