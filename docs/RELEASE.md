# 发版流程（Gitea Actions）

本仓库使用 **Gitea Actions** 进行发版与镜像构建。

## 触发方式

- `main` 分支变更 **不会** 自动发版。
- **推送 SemVer tag（`x.y.z`）** 才会触发发版流水线。

## 发版前检查

1. 确认本地 `main` 已同步到远端：

```bash
git checkout main
git pull gitea main
```

2. 确认 Gitea 仓库已配置 Actions Secrets（名称需一致）：

- `RELEASE_TOKEN`：用于调用 Gitea API 创建 release
- `DOCKERHUB_USERNAME`：Docker Hub 用户名
- `DOCKERHUB_TOKEN`：Docker Hub token

## 发版步骤

1. 选定版本号（SemVer：`x.y.z`）
2. 创建并推送 tag（推荐带注释的 tag）：

```bash
git tag -a 0.2.3 -m "Release 0.2.3"
git push gitea 0.2.3
```

## 发版产物

Actions 成功后会产出：

- Gitea Release：tag 名即版本号，release notes 由流水线自动生成
- Docker Hub 镜像（仅 `linux/amd64`）：
  - `${DOCKERHUB_USERNAME}/gemini2api:0.2.3`
  - `${DOCKERHUB_USERNAME}/gemini2api:latest`

## 常见操作

### 给指定 commit 打 tag

```bash
git tag -a 0.2.3 <commit-sha> -m "Release 0.2.3"
git push gitea 0.2.3
```

### 重新触发发版

- 直接重新运行对应的 Actions workflow（或使用 `workflow_dispatch` 并填写 `tag`）。
- workflow 会 `checkout` 到该 tag 对应的 commit，然后重新构建并 push 镜像；创建 release 的步骤是幂等的（已存在会跳过）。

