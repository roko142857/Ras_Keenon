# 从机器人推送到 GitHub

工作区：`/home/peanut/ras_keenon_ws`。仓库：[roko142857/Ras_Keenon](https://github.com/roko142857/Ras_Keenon)。

## 已配置的推送方式

`origin` 使用 GitHub 的 SSH 443 端口：

```text
ssh://git@ssh.github.com:443/roko142857/Ras_Keenon.git
```

机器人通过 SSH agent forwarding 使用开发电脑已有的 GitHub SSH 身份。私钥保留在开发电脑，转发只在 SSH 连接期间有效。

在已有 GitHub SSH 身份的开发电脑上执行：

```bash
ssh-add -l
ssh -A peanut@192.168.3.20
```

如果 `ssh-add -l` 提示没有身份，先在开发电脑执行 `ssh-add ~/.ssh/id_ed25519`。如果提示无法连接 agent，先执行 `eval "$(ssh-agent -s)"`，再加载密钥。

登录机器人后执行：

```bash
cd /home/peanut/ras_keenon_ws
git status
git add .
git diff --cached --stat
git commit -m "Describe the workspace changes"
git push
```

`git add .` 会按照 `.gitignore` 排除构建目录、运行数据、诊断证据、备份和本地环境文件。提交前查看暂存内容。没有新改动时可直接 `git push`。

## 常见问题

- `could not read Username`：HTTPS 没有可用凭据。检查 `git remote -v`，应为上面的 SSH 地址。
- `Permission denied (publickey)`：先确认通过 `ssh -A` 登录，再在机器人执行 `ssh-add -l` 检查转发的身份。
- 机器人直接登录、未转发 agent 的 SSH 会话以及无人值守任务，不能使用开发电脑的身份推送。如果需要机器人独立推送，需要另配仓库写入密钥。
- 连接超时：在机器人执行 `ssh -T -p 443 git@ssh.github.com` 检查 SSH 443 通道。正常认证会显示 `Hi roko142857!`；GitHub 不提供交互 shell，因此该测试成功时退出码仍为 1。

## 2026-11-05 诊断结果

本次检查中 DNS 解析、GitHub TCP 22/443 和 HTTPS 访问正常；HTTPS 仓库读取正常，但推送缺少用户名/令牌。原有提交仅包含 README，源码尚未加入版本控制。修复使用已有 SSH 身份转发，并提交工作区源码、测试与文档。

官方说明：[SSH agent forwarding](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/using-ssh-agent-forwarding)、[SSH 443 端口](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)。
