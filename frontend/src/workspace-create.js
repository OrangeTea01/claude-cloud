/** A workspace keeps its mode for its entire lifetime. */
export function createSoloWorkspaceForm(page = document) {
  return new Promise((resolve) => {
    const dialog = page.createElement('dialog');
    dialog.className = 'modal workspace-create-modal';
    dialog.innerHTML = `<form method="dialog"><div class="modal-heading"><h2>新建工作区</h2><button type="button" class="icon-button" data-dismiss aria-label="关闭">×</button></div>
      <label class="field-label" for="workspace-create-name">工作区名称</label><input class="text-input" id="workspace-create-name" maxlength="60" autocomplete="off" required>
      <fieldset class="workspace-mode-options"><legend>使用方式</legend>
        <label><input type="radio" name="mode" value="code" checked><span><strong>编程</strong><small>修改文件、运行命令，完成项目。</small></span></label>
        <label><input type="radio" name="mode" value="chat"><span><strong>普通聊天</strong><small>问答、写作、看图和阅读文本，不执行工具。</small></span></label>
      </fieldset><p class="modal-description">创建后模式保持不变。需要另一种模式时，新建一个工作区即可。</p>
      <p class="form-error" role="alert"></p><div class="modal-footer"><button type="button" class="button secondary" data-dismiss>取消</button><button class="button primary" type="submit">创建</button></div></form>`;
    let result = null;
    page.body.append(dialog);
    dialog.querySelectorAll('[data-dismiss]').forEach((button) => button.addEventListener('click', () => dialog.close()));
    dialog.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      const name = dialog.querySelector('#workspace-create-name').value.trim();
      const mode = dialog.querySelector('input[name="mode"]:checked')?.value;
      if (!name || name.length > 60 || !['chat', 'code'].includes(mode)) {
        dialog.querySelector('.form-error').textContent = '请输入 1 至 60 个字符的名称，并选择使用方式。';
        return;
      }
      result = { name, mode };
      dialog.close();
    });
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); }, { once: true });
    dialog.showModal();
    dialog.querySelector('#workspace-create-name').focus();
  });
}
