import {store,reload} from '../stores/workspace.js';import {ledger,field,input,select,badge} from '../components/ledger.js';import {esc,money,date} from '../utils/format.js';import {bindForm,request} from '../utils/forms.js';

const encState={held:'占用中',released:'已退回',settled:'已核销',transferred:'已迁移转出'};

function migrationKey(){return 'ui-'+(crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(16).slice(2));}

export function render(){
 const d=store.data;
 const funded=d.treatments.filter(t=>t.status==='funded');
 const grantName=id=>esc(d.grants.find(g=>g.id===id)?.name||'—');
 const unsigned=d.steps.filter(s=>!s.done);
 const money2=g=>money(g.budget_cents-g.reserved_cents-g.spent_cents);
 const grantOptions=d.grants.map(g=>`<option value="${g.id}">${esc(g.name)} · 可用 ${money2(g)}</option>`).join('');
 const strips=d.grants.map(g=>`<section class="grant-strip"><div><small>${esc(g.code)}</small><h3>${esc(g.name)}</h3></div><dl><div><dt>预算</dt><dd>${money(g.budget_cents)}</dd></div><div><dt>占用</dt><dd>${money(g.reserved_cents)}</dd></div><div><dt>已核销</dt><dd>${money(g.spent_cents)}</dd></div><div><dt>当前可用</dt><dd>${money2(g)}</dd></div></dl></section>`).join('');
 const stepOptions=unsigned.map(s=>`<option value="${s.id}" data-treatment="${s.treatment_id}">#${s.id} 方案#${s.treatment_id} / ${esc(s.name)} · ${money(s.amount_cents)} · ${grantName(s.grant_id)}</option>`).join('');
 const fundPlanOptions=d.treatments.filter(t=>t.status==='draft').map(t=>`<option value="${t.id}">#${t.id} ${esc(t.name)} / ${money(t.estimated_cents)}</option>`).join('');
 const fundedOptions=funded.map(t=>`<option value="${t.id}">#${t.id} ${esc(t.name)}</option>`).join('');
 const transferRows=d.encumbrance_transfers.slice().reverse().map(t=>[date(t.at),'#'+t.treatment_id,`${grantName(t.source_grant_id)} → ${grantName(t.target_grant_id)}`,money(t.amount_cents)+`<strong>原方案保留 ${money(t.retained_cents)}</strong>`,`${money(t.source_available_before)} → ${money(t.source_available_after)}`,`${money(t.target_available_before)} → ${money(t.target_available_after)}`,`已签证 #${t.signed_step_ids.join(',')||'—'} 留原专项<br>迁出工序 ${t.migrated_positions.map(p=>'第'+p+'道').join('、')||'—'}`]);
 const stepRows=funded.map(t=>{
  const steps=d.steps.filter(s=>s.treatment_id===t.id);
  return steps.map((s,i)=>`<tr>${i===0?`<td rowspan="${steps.length}">#${t.id} ${esc(t.name)}</td>`:''}<td>第${s.position}道 ${esc(s.name)}</td><td>${badge(s.done?'已签证':'未施工',s.done?'good':'')}</td><td>${grantName(s.grant_id)}</td><td>${money(s.amount_cents)}</td></tr>`).join('');
 }).join('');
 const stepTable=funded.length?`<div class="table-scroll"><table><thead><tr><th>方案</th><th>工序</th><th>签证</th><th>承款专项</th><th>工序占款</th></tr></thead><tbody>${stepRows}</tbody></table></div>`:'<p class="empty">暂无施工中方案。</p>';
 const encRows=d.encumbrances.map(e=>{const t=d.encumbrance_transfers.find(v=>v.source_encumbrance_id===e.id);const amount=t?`${money(e.amount_cents)}<strong>迁移前 ${money(t.original_amount_cents)}</strong>`:money(e.amount_cents);return ['#'+e.treatment_id,grantName(e.grant_id),amount,e.actual_cents===null?'—':money(e.actual_cents),badge(encState[e.status]||e.status)];});
 return `<h2>专项经费台账</h2><p class="intro">施工前占用估价，逐道签证后按实际支出核销。余额不足时保留草案，可改价或改选专项。未施工工序可整批迁至另一专项，已签证与已核销部分留在原专项。</p>${strips}<div class="forms-two"><form id="fund"><h3>批准占款开工 · 主管</h3>${field('待占款方案',select('id',fundPlanOptions))}${field('专项',select('grant_id',d.grants.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('')))}<button>原子占用经费</button></form><form id="settle"><h3>核销施工费用 · 主管</h3>${field('施工中方案',select('id',fundedOptions))}${field('实际支出 / 元',input('yuan','number','2500','min="1" step="0.01"'))}<button>完成核销</button><p class="hint">核销先冲清各专项已占金额，余额按占款比例分摊到承款专项。</p></form></div><section><form id="migrate"><h3>未施工工序迁移占款 · 主管</h3><p class="hint">把尚未签证工序的占款整批移到目标专项；已签证工序和已核销记录留在原专项。目标专项余额不足或迁移期间新增签证时，本次迁移整次不生效。</p><div class="forms-two compact">${field('施工中方案',select('treatment_id',fundedOptions))}${field('原专项',`<select name="source_grant_id"><option value="">自动识别（按所选工序）</option>${grantOptions}</select>`)}${field('目标专项',select('target_grant_id',grantOptions))}</div>${field('待迁移工序（可不选，默认该方案全部未施工工序）',`<select name="step_ids" id="mig-steps" multiple size="5">${stepOptions}</select>`)}<input type="hidden" name="idempotency_key" value="${migrationKey()}"><button>一次性切换专项占款</button></form></section><h3>迁移存根 · 前后对应</h3>${ledger(['时间','方案','专项对应','迁移金额','原专项余额（前→后）','目标专项余额（前→后）','工序'],transferRows,'暂无迁移记录。')}<h3>方案工序承款</h3>${stepTable}<details><summary>设立新的专项经费</summary><form id="grant" class="inline-form">${field('编号',input('code','text','GF-2027'))}${field('用途',input('name','text','珍本应急修复'))}${field('预算 / 元',input('yuan','number','8000','min="1" step="0.01"'))}<button>设立专项</button></form></details><h3>占款与核销流水</h3>${ledger(['方案','专项','占用','实际核销','状态'],encRows)}`;
}

export function bind(r){
 bindForm('fund',d=>request('/treatments/'+d.id+'/fund',{grant_id:+d.grant_id}),r);
 bindForm('settle',d=>request('/treatments/'+d.id+'/settle',{actual_cents:Math.round(+d.yuan*100)}),r);
 bindForm('grant',d=>request('/grants',{code:d.code,name:d.name,budget_cents:Math.round(+d.yuan*100)}),r);
 const form=document.getElementById('migrate');
 if(!form)return;
 const plan=form.querySelector('select[name=treatment_id]');
 const source=form.querySelector('select[name=source_grant_id]');
 const steps=document.getElementById('mig-steps');
 const filter=()=>{for(const option of steps.options){const show=option.dataset.treatment===plan.value;option.hidden=!show;option.disabled=!show;if(!show)option.selected=false;}};
 // 未施工工序都属同一专项时自动带出原专项；分属多个专项时留空，由服务端提示指定。
 plan.onchange=()=>{filter();const grants=[...new Set(store.data.steps.filter(v=>v.treatment_id===+plan.value&&!v.done).map(v=>v.grant_id).filter(Boolean))];source.value=grants.length===1?String(grants[0]):'';};
 filter();
 form.onsubmit=async event=>{
  event.preventDefault();
  const button=form.querySelector('button');button.disabled=true;
  document.getElementById('notice').textContent='正在切换专项占款…';
  try{
   const treatment=+plan.value;
   const selected=[...steps.selectedOptions].filter(o=>!o.disabled).map(o=>+o.value);
   // 以页面打开时的签证快照提交；迁移期间若新增签证，服务端整次拒绝。
   const signed=store.data.steps.filter(s=>s.treatment_id===treatment&&s.done).map(s=>s.id);
   await request('/treatments/'+treatment+'/migrate',{
    target_grant_id:+form.querySelector('select[name=target_grant_id]').value,
    source_grant_id:source.value?+source.value:null,
    step_ids:selected.length?selected:null,
    expected_signed_step_ids:signed,
    idempotency_key:form.querySelector('input[name=idempotency_key]').value});
   await reload();r('迁移已生效：未施工工序已转由目标专项承款，存根留档');
  }catch(error){document.getElementById('notice').textContent=error.message;document.getElementById('notice').className='notice error';}
  finally{button.disabled=false;}
 };
}
