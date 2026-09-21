# 从建仓到推送：傻瓜式步骤

适用环境：Windows + PowerShell，本项目 `step-tool-proxy`。
全程只需要复制粘贴命令，每一步都写了"应该看到什么"。

---

## 第 0 步：确认工具已安装

```powershell
git --version
```

应该看到类似 `git version 2.55.0.windows.5` 的版本号。
如果提示找不到命令，先去 https://git-scm.com/download/win 安装 Git，安装时一路下一步即可。

---

## 第 1 步：告诉 Git 你是谁（只需做一次）

```powershell
git config --global user.name "你的GitHub用户名"
git config --global user.email "你的邮箱"
```

验证：

```powershell
git config --global user.name
git config --global user.email
```

> 本机当前已配置为：用户名 `Onswayn`，邮箱 `219408803+OnsWayn@users.noreply.github.com`，可跳过本步。

---

## 第 2 步：在项目文件夹里建仓

```powershell
cd C:\Users\qq459\Desktop\del\10\step-tool-proxy
git init -b main
```

应该看到 `Initialized empty Git repository in .../.git/`。
`-b main` 表示主分支叫 `main`（GitHub 现在默认也是这个名字）。

---

## 第 3 步：确认密钥文件不会被提交（重要！）

项目根目录已有 `.gitignore`，里面写明了这些内容不进 Git：

- `data/` 下的密钥文件（`master.token`、`tokens.json`、`upstreams.json`、`settings.json`）
- `.env`、`*.token`
- `__pycache__`、`.venv` 等

验证一下：

```powershell
git status --porcelain
```

逐行看输出，**确认里面没有** `.env`、`data/master.token`、`data/tokens.json` 这类文件。
只应该看到 `.env.example`（本项目是 `config.example.env`）、源代码、README 等。

如果不小心看到了密钥文件，先停下来检查 `.gitignore`，不要继续。

---

## 第 4 步：第一次提交

```powershell
git add -A
git commit -m "Initial commit: step-tool-proxy"
```

应该看到一长串 `create mode ...` 的文件列表，最后是 `N files changed, ...`。
提交完再确认工作区是干净的：

```powershell
git status
```

应该显示 `nothing to commit, working tree clean`。

---

## 第 5 步：在 GitHub 网页上建一个空仓库

1. 浏览器打开 https://github.com/new
2. **Repository name** 填 `step-tool-proxy`
3. Public / Private 自选（有密钥历史的一律选 **Private**）
4. **不要勾选** "Add a README file"、"Add .gitignore"、"Choose a license"
   （勾了会和本地冲突，新手最容易踩这个坑）
5. 点 **Create repository**

创建成功后会进入一个空仓库页面，页面上有一行以 `https://github.com/你的用户名/step-tool-proxy.git` 结尾的地址，复制它。

---

## 第 6 步：把本地仓库关联到 GitHub

```powershell
git remote add origin https://github.com/你的用户名/step-tool-proxy.git
```

验证：

```powershell
git remote -v
```

应该看到两行 `origin`，后面跟着你刚填的地址。

---

## 第 7 步：推送

```powershell
git push -u origin main
```

第一次推送会弹窗要求登录 GitHub：

- 弹窗里点 **"Sign in with your browser"**（浏览器登录最省事），或者
- 用 Personal Access Token 当密码（GitHub 从 2021 年起不再支持账号密码）

推送成功应该看到类似：

```text
To https://github.com/你的用户名/step-tool-proxy.git
 * [new branch]      main -> main
branch 'main' set up to track 'origin/main'.
```

`-u` 参数只需加这一次，以后直接 `git push` 就行。

---

## 第 8 步：验证

刷新 GitHub 仓库页面，应该能看到所有项目文件（README、代码、`config.example.env` 等）。
**没有** `.env` 和 `data/` 里的密钥文件就对了。

---

## 以后每次更新项目（日常三步）

```powershell
git add -A
git commit -m "写清楚这次改了什么"
git push
```

---

## 出事了怎么办

### 推送被拒：`rejected - fetch first`

说明 GitHub 上的仓库不是空的（比如建仓时勾了 README）。二选一：

**情况 A：GitHub 上那些文件你不要**

```powershell
git push -u origin main --force
```

**情况 B：想保留 GitHub 上的文件**

```powershell
git pull origin main --rebase
git push
```

### 发现密钥被误提交进 Git 了

1. 先当密钥已泄露：去 WebUI / StepFun 后台**吊销并更换**所有密钥
2. 再从 Git 里删掉并补一条 `.gitignore`：

```powershell
git rm --cached .env data/master.token
# 确认 .gitignore 已覆盖这些文件后
git commit -m "Remove secrets from tracking"
git push
```

注意：这样只能让最新版本不含密钥，**历史记录里仍然有**。要彻底抹掉需要用 `git filter-repo` 工具重写历史，或者最简单——删掉整个仓库重建（密钥已经换过的情况下）。

### 本地分支名不对（比如叫 master 而远端要 main）

```powershell
git branch -M main
git push -u origin main
```

---

## 一页速查

| 步骤 | 命令 |
|------|------|
| 建仓 | `git init -b main` |
| 暂存所有改动 | `git add -A` |
| 提交 | `git commit -m "说明"` |
| 关联 GitHub | `git remote add origin https://github.com/用户名/仓库名.git` |
| 首次推送 | `git push -u origin main` |
| 日常推送 | `git push` |
| 看状态 | `git status` |
| 看历史 | `git log --oneline` |
