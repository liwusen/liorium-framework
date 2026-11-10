# 发布到 PyPI（Trusted Publishing）

Liorium 只发布**一个**发行版：`liorium`。12 个模块（`core` / `vfs` / `realm_base` /
`core_tool` / `core_tool_realm` / `agent` / `memory` / `skills` / `mcp` / `todo_realm` /
`provider` / `cli`）的源码由根 `pyproject.toml` 的 `force-include` 打进**同一个 wheel**，
所以 Trusted Publishing **只需要填一次表单**。

工作流：[`.github/workflows/publish.yml`](../.github/workflows/publish.yml)

```
tag v0.1.0 ──▶ build（1 个 sdist + 1 个 wheel，内含 12 个模块
                    + twine check + check_wheel 验模块齐全）
            └▶ publish（OIDC 换一次性上传凭证，1 条 publisher）
               └▶ release（把 sdist/wheel 挂到 GitHub Release）
```

## 一、PyPI 侧一次性配置（只有 1 条）

打开 <https://pypi.org/manage/account/publishing/> → **Add a new pending publisher → GitHub**，
填这一条就好：

| 字段 | 值 |
|---|---|
| PyPI Project Name | `liorium` |
| Owner | `liwusen` |
| Repository name | `liorium-framework` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

项目还不存在，所以用 *pending* publisher；第一次成功发布后它会自动转成普通 publisher，
以后不用再配。

> **为什么现在只需要 1 条。** PyPI 对 GitHub publisher 的唯一性约束是
> `(owner, repo, workflow, environment)` 这个四元组的**全局唯一**：一个四元组只能登记一个项目。
> 以前打算发 12 个 `liorium-*` 包，同仓库 + 同 workflow 就只能靠 12 个不同的 environment 区分，
> 少填一条就会得到
> `A pending trusted publisher matching this configuration has already been registered for a different project name.`
> 现在只有一个发行版，这条约束天然满足 —— 一个项目、一条 publisher、一个 `pypi` 环境。
> 背景见 [pypi/warehouse#16920](https://github.com/pypi/warehouse/issues/16920)。

**TestPyPI 是可选的**：想先演练，就在
<https://test.pypi.org/manage/account/publishing/> 用同样的项目名 `liorium`，
environment 填 `testpypi`，然后手动触发 workflow 选 `testpypi`。
TestPyPI 与 PyPI 是两套独立登记，配与不配都不影响正式发布。

**想连 PyPI 都不碰先验证构建**：手动触发 workflow 选 `build-only` —— 只跑
`uv build` + `twine check --strict` + `check_wheel.py`（验 wheel 里 12 个模块齐全），
不发任何东西。

> 名字写错任何一个字符都会得到 `invalid-publisher` 错误，且**不会**退化成密码登录。

## 二、发一个版本

版本号只有两处，必须一致（`scripts/check_version.py` 会强制校验，不一致直接失败）：

1. 根 `pyproject.toml` 的 `[project].version`
2. `src/liorium/__init__.py` 的 `__version__`

```bash
# 两处都改成 0.1.0 之后
git commit -am "release: v0.1.0"
git tag v0.1.0
git push origin main --tags
```

推送 `v*` tag 即触发发布。同版本已存在于 PyPI 时 `skip-existing: true` 会跳过而不是报错，
所以重跑是安全的（Actions 页面 **Re-run failed jobs**）。

手动触发（Actions → Publish to PyPI → Run workflow）可指定 `pypi` / `testpypi` / `build-only`。
推荐顺序：先 `build-only` 跑绿 → 配好那 1 条 publisher → 推 `v*` tag 真发。

## 三、本地预跑同样的步骤

```powershell
# 版本一致性（--all 顺便查 12 个 workspace 成员有没有漂）
uv run --no-project python scripts/check_version.py v0.1.0 --all

# 构建 1 个 sdist + 1 个 wheel，并检查元数据与模块齐全
uv build --out-dir dist
uv run --no-project --with twine twine check --strict dist/*
uv run --no-project python scripts/check_wheel.py dist/*.whl

# 用 TestPyPI 试发（需要自己账号的 token，仅本地演练用）
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-XXX dist/*
```

装下来验一遍：

```powershell
uv run --no-project python -c "import liorium, liorium.agent, liorium.cli; print(liorium.__version__)"
```

## 四、为什么是「一个发行版、12 个模块」

- 源码仍按 `packages/<包名>/src/liorium/<子包>` 分目录维护：依赖方向、单元测试、
  `tests/test_architecture.py` 的架构守卫都按模块边界工作，改动面小。
- 发布时由根 `pyproject.toml` 的 `force-include` 把 12 棵树注入同一个 wheel，
  wheel 里就是 `liorium/{core,vfs,agent,...}`，**import 路径与开发时完全一致**。
- 第三方依赖靠 extra 分档（`[all]` / `[mcp]` / `[vector]` / `[openai]` / `[anthropic]` /
  `[google]` / `[images]`）；`core` 与 `vfs` 的**代码**仍然零第三方依赖
  （`tests/test_architecture.py` 钉着），只想要 URI / VFS 时可以把这两个目录单独 vendored。
- 两条守卫防「发出去少东西」：`tests/test_distribution.py`（force-include 覆盖 12 个模块 +
  版本一致 + 根项目声明了成员包用到的全部第三方依赖）、`scripts/check_wheel.py`
  （对构建产物再验一次模块清单与 entry point）。

## 五、为什么不建 `release-package.yml`

旧式做法（`twine upload` + `PYPI_API_TOKEN` secret）把长期凭据放进仓库；Trusted Publishing
把凭据换成"这个 workflow、这个仓库、这个 environment 才能换到"的一次性 token，
泄露面从"永久 token"缩到"一次构建"，因此本仓库只保留 OIDC 这一条发布路径。

## 六、常见失败

| 报错 | 原因 |
|---|---|
| `invalid-publisher` / `OIDC token exchange failed` | PyPI 侧的 owner / repo / workflow / environment 与 workflow 不一致（逐字符比对，含大小写）；workflow 里是 `environment: pypi` |
| `A pending trusted publisher matching this configuration has already been registered for a different project name.` | 这个四元组已经登记给别的项目了。本项目只需 1 条（`liorium` + `pypi`）；如果你之前按旧方案登记过 `liorium-core` 之类，去 publishing 页面删掉那条 |
| `File already exists` | 同版本已上传过；确认 `skip-existing` 生效，或改版本号 |
| `twine check` 失败 | `README.md` 的相对链接失效或缺少 `readme` 字段 |
| `check_wheel.py` 报缺模块 | 新增了 `packages/*/src/liorium/<子包>` 但没在根 `pyproject.toml` 的 `force-include` 里加映射 |
| 装下来 `import liorium.mcp` 报缺 `mcp` | 那是 extra：`pip install "liorium[mcp]"`（或 `[all]` 一次装齐） |
