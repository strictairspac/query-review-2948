import{indexTrials}from'../stores/index_trial.js';
import{getTrial,createTrial,decideTrial,cleanupTrial,exportTrial}from'../api/index_trial.js';
import{samples}from'../stores/sample.js';
import{session}from'../stores/session.js';
import{header}from'../components/common/header.js';
import{table}from'../components/common/table.js';
import{esc,json}from'../utils/html.js';
import{notify,submit}from'../hooks/feedback.js';
import{TRIAL_STATUSES,TRIAL_DECISIONS}from'../constants/enums.js';

const COLUMNS=['customer','route','amount','temperature','status'];
const STATUS_CLASS={pending:'queued',building:'running',verifying:'running',verified:'different',retained:'passed',cleaning:'running',cleaned:'passed',failed:'failed'};
const badge=s=>`<span class="badge ${STATUS_CLASS[s]||''}">${esc(TRIAL_STATUSES[s]||s)}</span>`;

function planCard(title,plan){return`<div><h3>${esc(title)}</h3>${plan?`<p class="helper">采集耗时 ${esc(plan.elapsed_ms)} ms · 结果 ${esc(plan.row_count)} 行 · 结果摘要 <code>${esc((plan.result_checksum||'').slice(0,16))}…</code></p><pre class="json">${json(plan.plan)}</pre>`:'<p class="helper">尚未采集。</p>'}</div>`}

function detailHtml(t,isAdmin){
  const cmp=t.comparison;
  const canDecide=t.status==='verified'&&isAdmin;
  const events=(t.events||[]).map(e=>`<li><span class="mono">${esc(e.created_at)}</span> ${badge(e.to_status||'')} ${esc(e.reason)} <small>· ${esc(e.actor)}</small></li>`).join('');
  const decisionHtml=t.decision?`<section class="panel subtle"><h2>复核决定</h2><p><strong>${esc(TRIAL_DECISIONS[t.decision]||t.decision)}</strong> · ${esc(t.decision_by||'')} · ${esc(t.decided_at||'')}</p><p class="helper">原因：${esc(t.decision_reason||'')}</p><p class="helper">决定依据证据摘要 <code>${esc(t.decision_evidence_sha256)}</code>${t.decision_evidence_sha256===t.evidence_sha256?'（与本次验证证据一致）':'（与验证证据不一致，需复核）'}</p></section>`:'';
  const benchmark=cmp?`<div class="metrics"><div><small>估算总代价变化</small><strong>${cmp.cost_delta_ratio==null?'—':esc(cmp.cost_delta_ratio)+'%'}</strong></div><div><small>执行耗时变化</small><strong>${cmp.execution_delta_ratio==null?'—':esc(cmp.execution_delta_ratio)+'%'}</strong></div><div><small>建后是否走索引</small><strong>${cmp.index_used_after?'是':'否'}</strong></div><div><small>结果摘要一致</small><strong>${cmp.results_unchanged?'是':'否'}</strong></div></div><p class="helper">${esc(cmp.verdict)}<br>${esc(cmp.note)}</p>`:'';
  const decideForm=canDecide?`<form id="decide-${t.id}" class="panel subtle"><h2>复核并发布决定</h2><label>决定</label><select name="decision"><option value="retain">保留索引</option><option value="cleanup">清理索引</option></select><label>原因（与决定一并发布）</label><input name="reason" required maxlength="500" placeholder="例如：结算汇总查询确认走索引，保留"><div class="actions"><button class="primary" type="submit">发布决定 →</button></div><p class="helper">决定会绑定本次验证证据摘要 <code>${esc(t.evidence_sha256||'')}</code>；证据不同则需重新复核。</p></form>`:'';
  const cleanupLabel={failed:'手动清理本次残留索引',verified:'不经保留直接清理该索引',retained:'撤销保留并清理该索引'}[t.status];
  const cleanupForm=cleanupLabel&&isAdmin?`<form id="cleanup-${t.id}"><label>${cleanupLabel}</label><input name="reason" required maxlength="500" value="${t.status==='failed'?'失败残留清理':t.status==='retained'?'撤销保留，清理索引':'人工清理索引'}"><div class="actions"><button type="submit">提交清理 →</button></div></form>`:'';
  return`<section class="panel trial-detail" id="trial-${t.id}">
  <div class="actions" style="margin-top:0"><h2 style="margin:0">索引试验 #${t.id}</h2>${badge(t.status)}<button id="export-${t.id}">下载证据 JSON ↓</button></div>
  <p class="helper">列组 <code>${esc(t.column_group)}</code> · 索引 <code>${esc(t.index_name)}</code> · 归属 <code>${esc(t.index_ownership?.attribution||'')}</code> · 发起人 ${esc(t.requested_by)}</p>
  <p class="helper">发起原因：${esc(t.requested_reason||'（未填写）')}</p>
  ${t.last_error?'<p class="error">'+esc(t.last_error)+'</p>':''}
  ${t.plan_before||t.plan_after?`<div class="split">${planCard('建前实际计划',t.plan_before)}${planCard('建后实际计划',t.plan_after)}</div>${benchmark}`:'<p class="helper">建前/建后计划将在 worker 完成后显示。</p>'}
  ${t.evidence_sha256?`<p class="helper">本次验证证据 SHA-256 <code>${esc(t.evidence_sha256)}</code></p>`:''}
  ${decideForm}${cleanupForm}${decisionHtml}
  <h3>状态轨迹</h3><ul class="helper timeline">${events||'<li>暂无记录</li>'}</ul></section>`}

export async function indexTrialsPage(el){
  const rows=await indexTrials.load();
  const isAdmin=session.get().role==='admin';
  const sampleRows=await samples.load();
  const selected=Number(location.hash.split('/')[1])||rows[0]?.id;
  const detail=selected?await getTrial(selected):null;
  const form=isAdmin?`<form id="trial-form"><label>业务列（选 2–4 列，按字典序组成普通组合索引）</label><div class="checks">${COLUMNS.map(c=>`<label class="inline"><input type="checkbox" name="columns" value="${c}"> ${esc(c)}</label>`).join('')}</div><label>基准样本（采集建前/建后计划）</label><select name="sample_id">${sampleRows.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select><label>发起原因</label><input name="reason" required maxlength="500" placeholder="例如：结算汇总按客户与温度过滤，评估组合索引"><div class="actions"><button class="primary" type="submit">发起试验 →</button></div><p class="helper">同一列组并发发起只建一份索引；重复提交会复用进行中的试验。使用 CREATE INDEX CONCURRENTLY，构建期间账单仍可写，原有查询保持只读。</p></form>`:'<p class="helper">仅管理员可发起试验与发布保留/清理决定；其他角色可查看状态、原因与下载证据。</p>';
  el.innerHTML=header('05','用真实执行计划验证普通组合索引','管理员选择业务列发起试验；建索引期间账单仍可写，原有查询保持只读。建前建后计划、比较结果与保留/清理决定都以同一试验证据发布。')+`<div class="split indexsplit"><section class="panel"><h2>${isAdmin?'发起索引试验':'试验记录'}</h2>${form}<h3>试验列表</h3>${table(['编号 / 列组','状态','发起原因','查看'],rows.map(t=>['<span class="mono">#'+t.id+'</span><br>'+esc(t.column_group),badge(t.status),esc(t.requested_reason||'—'),`<a href="#index-trials/${t.id}">查看试验 →</a>`]),'还没有索引试验。管理员在上方选择业务列后发起。')}</section><aside class="panel">${detail?`<p class="helper">当前查看试验 #${detail.id}，完整计划与轨迹见下方。</p><a href="#index-trials">刷新试验列表 ↻</a>`:'<div class="empty">尚未发起索引试验。</div>'}</aside></div>${detail?detailHtml(detail,isAdmin):''}`;
  if(isAdmin)el.querySelector('#trial-form').onsubmit=e=>{e.preventDefault();const f=e.target;submit(f,async()=>{const cols=[...f.querySelectorAll('input[name=columns]:checked')].map(x=>x.value);if(cols.length<2)throw new Error('请至少选择 2 个业务列');const res=await createTrial({columns:cols,sample_id:Number(f.sample_id.value),reason:f.reason.value});notify(res.reused?'该列组已有进行中的试验 #'+res.trial.id+'，已复用':'试验 #'+res.trial.id+' 已排队');location.hash='index-trials/'+res.trial.id})};
  if(!detail)return;
  const ex=el.querySelector('#export-'+detail.id);if(ex)ex.onclick=()=>exportTrial(detail.id).catch(e=>notify(e.message));
  const d=el.querySelector('#decide-'+detail.id);if(d)d.onsubmit=e=>{e.preventDefault();submit(d,async()=>{const f=new FormData(d);const res=await decideTrial(detail.id,{decision:f.get('decision'),reason:f.get('reason'),evidence_sha256:detail.evidence_sha256});notify('决定已发布：'+(TRIAL_DECISIONS[res.decision]||res.decision));location.hash='index-trials/'+detail.id;await indexTrialsPage(el)})};
  const c=el.querySelector('#cleanup-'+detail.id);if(c)c.onsubmit=e=>{e.preventDefault();submit(c,async()=>{await cleanupTrial(detail.id,{reason:new FormData(c).get('reason')});notify('清理已排队');await indexTrialsPage(el)})};
}
