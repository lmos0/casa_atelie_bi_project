const form = document.querySelector('#upload-form');
const files = document.querySelector('#files');
const button = document.querySelector('#submit');
const result = document.querySelector('#result');
const clearOpen = document.querySelector('#clear-open');
const clearDialog = document.querySelector('#clear-dialog');
const clearCancel = document.querySelector('#clear-cancel');
const clearConfirm = document.querySelector('#clear-confirm');
const clearError = document.querySelector('#clear-error');
const clearResult = document.querySelector('#clear-result');
let clearing = false;

clearOpen.addEventListener('click', () => {
  clearError.hidden = true;
  clearDialog.showModal();
});
clearCancel.addEventListener('click', () => clearDialog.close());
clearDialog.addEventListener('cancel', event => {
  if (clearing) event.preventDefault();
});
clearConfirm.addEventListener('click', async () => {
  if (clearing) return;
  clearing = true;
  clearConfirm.disabled = clearCancel.disabled = button.disabled = clearOpen.disabled = true;
  clearConfirm.textContent = 'Limpando…';
  clearError.hidden = clearResult.hidden = true;
  try {
    const response = await fetch('/api/limpar', {
      method: 'POST',
      headers: {'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.erro || 'Não foi possível limpar o banco.');
    clearDialog.close();
    result.hidden = true;
    clearResult.className = 'success';
    clearResult.textContent = data.mensagem;
    clearResult.hidden = false;
  } catch (error) {
    clearError.textContent = error.message || 'A conexão foi interrompida. Confira os registros antes de tentar novamente.';
    clearError.hidden = false;
  } finally {
    await refresh();
    clearing = false;
    clearConfirm.disabled = clearCancel.disabled = button.disabled = clearOpen.disabled = false;
    clearConfirm.textContent = 'Limpar banco';
  }
});
const labels = {oferta_turmas: 'Turmas', interesses: 'Interesses', inscricoes: 'Inscrições', pagamentos: 'Pagamentos'};
function message(text, error = false) {
  result.hidden = false;
  result.className = error ? 'error' : 'success';
  result.textContent = text;
}
files.addEventListener('change', () => {
  document.querySelector('#selection').textContent = [...files.files].map(f => f.name).join(' · ') || 'Nenhum arquivo selecionado.';
});
async function refresh() {
  try {
    const response = await fetch('/api/resumo');
    const data = await response.json();
    if (!response.ok) throw new Error(data.erro);
    document.querySelector('#database-error').textContent = '';
    const counts = document.querySelector('#counts'); counts.replaceChildren();
    Object.entries(labels).forEach(([key, label]) => {
      const metric = document.createElement('div'); metric.className = 'metric';
      const number = document.createElement('strong'); number.textContent = data.contagens[key].toLocaleString('pt-BR');
      const caption = document.createElement('span'); caption.textContent = label;
      metric.append(number, caption); counts.append(metric);
    });
    const history = document.querySelector('#history'); history.replaceChildren();
    document.querySelector('#empty-history').hidden = data.lotes.length > 0;
    document.querySelector('#history-table').hidden = data.lotes.length === 0;
    for (const batch of data.lotes) {
      const tr = document.createElement('tr');
      const statuses = {concluido:'Concluído', rejeitado:'Rejeitado', processando:'Processando'};
      for (const value of [batch.id_lote.slice(0, 8), new Date(batch.criado_em).toLocaleString('pt-BR'), statuses[batch.status] || batch.status, batch.resumo.inseridas ?? '—', batch.resumo.ignoradas ?? '—']) {
        const td = document.createElement('td'); td.textContent = value; tr.append(td);
      }
      tr.title = (batch.resumo.erros || []).join('; '); history.append(tr);
    }
  } catch (error) { document.querySelector('#database-error').textContent = error.message || 'Não foi possível consultar o banco.'; }
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (clearing || button.disabled) return;
  if (files.files.length > 4) return message('Selecione no máximo quatro arquivos.', true);
  if ([...files.files].reduce((total, file) => total + file.size, 0) > 10 * 1024 * 1024) return message('O lote excede 10 MB.', true);
  button.disabled = clearOpen.disabled = true; button.textContent = 'Validando e importando…';
  clearResult.hidden = true;
  result.hidden = true;
  try {
    const response = await fetch('/api/importar', {method:'POST', body:new FormData(form), headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}});
    const data = await response.json();
    if (!response.ok) message(data.erro || data.erros?.join(' ') || 'Falha na importação.', true);
    else message(`Importação concluída: ${data.inseridas} linhas inseridas e ${data.ignoradas} já existentes.`);
  } catch { message('A conexão foi interrompida. Confira o histórico antes de reenviar.', true); }
  finally { button.disabled = clearOpen.disabled = false; button.textContent = 'Importar'; await refresh(); }
});
refresh();
