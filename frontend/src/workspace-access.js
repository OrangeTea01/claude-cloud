const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));

/** Password values stay only in this short-lived form and its submit result. */
export function workspacePasswordForm({ create = false, protectedWorkspace = false } = {}) {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'modal workspace-password-modal';
    const field = (id, label, type = 'password', required = false, placeholder = '') => `<label class="field-label" for="${id}">${label}</label><input class="text-input" id="${id}" type="${type}" ${required ? 'required' : ''} maxlength="${type === 'text' ? 60 : 1024}" autocomplete="${id === 'workspace-current-password' ? 'current-password' : type === 'password' ? 'new-password' : 'off'}" placeholder="${placeholder}">`;
    dialog.innerHTML = `<form method="dialog"><div class="modal-heading"><h2>${create ? '新建工作区' : '工作区密码'}</h2><button type="button" class="icon-button" data-dismiss aria-label="关闭">×</button></div><p class="modal-description">${create ? '工作区只属于你的账号。可以额外设置密码，每次重新登录后解锁访问。' : protectedWorkspace ? '修改或取消密码需要验证当前密码。新密码留空可取消额外密码保护。' : '为这个工作区增加一道密码保护。账号登录后仍需解锁才能查看内容。'}</p>${create ? field('workspace-create-name', '工作区名称', 'text', true, '例如：我的个人网站') : ''}${protectedWorkspace ? field('workspace-current-password', '当前工作区密码', 'password', true) : ''}${field('workspace-new-password', create ? '工作区密码（可选）' : '新工作区密码', 'password', !create && !protectedWorkspace, '至少 8 个字符')}${field('workspace-confirm-password', '确认工作区密码')}<p class="form-error" role="alert"></p><div class="modal-footer"><button type="button" class="button secondary" data-dismiss>取消</button><button class="button primary" type="submit">${create ? '创建工作区' : '保存密码设置'}</button></div></form>`;
    document.body.append(dialog);
    let result = null;
    dialog.querySelectorAll('[data-dismiss]').forEach((button) => button.addEventListener('click', () => dialog.close()));
    dialog.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      const name = dialog.querySelector('#workspace-create-name')?.value.trim();
      const currentPassword = dialog.querySelector('#workspace-current-password')?.value || '';
      const newPassword = dialog.querySelector('#workspace-new-password').value;
      const confirmPassword = dialog.querySelector('#workspace-confirm-password').value;
      const error = create && !name ? '请输入工作区名称。' : newPassword && newPassword.length < 8 ? '工作区密码至少需要 8 个字符。' : newPassword !== confirmPassword ? '两次输入的密码不一致。' : protectedWorkspace && !currentPassword ? '请输入当前工作区密码。' : !create && !protectedWorkspace && !newPassword ? '请输入新的工作区密码。' : '';
      if (error) { dialog.querySelector('.form-error').textContent = error; return; }
      result = create ? { name, ...(newPassword ? { password: newPassword } : {}) } : { currentPassword, newPassword, confirmPassword };
      dialog.close();
    });
    dialog.addEventListener('close', () => {
      dialog.querySelectorAll('input').forEach((input) => { input.value = ''; });
      dialog.remove(); resolve(result);
    });
    dialog.showModal();
    dialog.querySelector('input').focus();
  });
}

export function lockedWorkspaceMarkup(workspace) {
  return `<section class="workspace-locked"><div class="workspace-lock-mark" aria-hidden="true">⌁</div><h1>${esc(workspace.name)}</h1><h2>工作区已锁定</h2><p>输入这个工作区的密码后查看文件和对话。<br>锁定不影响已经运行的任务。</p><form id="workspace-unlock-form"><label class="field-label" for="workspace-unlock-password">工作区密码</label><input id="workspace-unlock-password" class="text-input" type="password" maxlength="1024" autocomplete="current-password" required><p class="form-error" id="workspace-unlock-error" role="alert"></p><button class="button primary" type="submit">解锁工作区</button></form></section>`;
}
