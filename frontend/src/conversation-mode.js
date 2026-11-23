export function conversationMode(workspace, model) {
  return (model?.mode || workspace?.mode) === 'chat' ? 'chat' : 'code';
}

export const CHAT_ATTACHMENT_HINT = 'PNG/JPEG/GIF/WebP 每张 ≤ 5 MiB；UTF-8 文本每份 ≤ 256 KiB；每条最多 5 个';
export const CHAT_MODE_HELP = '普通聊天用于问答、写作和看图，不执行命令或修改文件。底层仍使用服务器上的 Claude Code，并使用你的 Claude 账号额度。\n\n可以发送 PNG、JPEG、GIF、WebP 图片（每张最多 5 MiB），或 UTF-8 文本（每份最多 256 KiB、合计最多 512 KiB），每条最多 5 个附件，合计最多 10 MiB。PDF、Office 和 ZIP 等文件请使用编程工作区处理。\n\n工作区的模式创建后保持不变。普通聊天不会读取未附在当前消息中的工作区文件；也不提供 claude.ai 官网的搜索或 Artifacts 功能。消息支持 Markdown，对话记录会保留。';
