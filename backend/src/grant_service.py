import hashlib
from sqlalchemy import select
from .models import Grant,Encumbrance,EncumbranceTransfer,Treatment,Step,Accession,Isolation
from .common import get,reject,finish,lock_treatment,audit,dump
from .catalog_service import reviewed_risk

def create_grant(db,data,user):
    reject(db.scalar(select(Grant).where(Grant.code==data.code)) is not None,'经费编号重复')
    row=Grant(**data.model_dump());db.add(row);db.flush();return finish(db,user,'设立专项经费',row)

def _steps(db,treatment_id):
    return list(db.scalars(select(Step).where(Step.treatment_id==treatment_id).order_by(Step.position,Step.id)))

def _split(total,count):
    # 估价按工序均摊，余数落在末道工序，便于逐道迁移与核销对账。
    if count<=0:return []
    base,extra=divmod(total,count)
    parts=[base]*count
    if extra:parts[-1]+=extra
    return parts

def _assign_steps(db,treatment_id,grant_id,total):
    for step,part in zip(_steps(db,treatment_id),_split(total,len(_steps(db,treatment_id)))):
        step.grant_id=grant_id;step.amount_cents=part

def _ensure_step_funding(db,treatment_id,enc):
    # 兼容升级前已占款的方案：旧工序没有逐道承款信息时，按原占款额一次性补记。
    steps=_steps(db,treatment_id)
    if steps and all(s.grant_id is None for s in steps):
        parts=_split(enc.amount_cents,len(steps))
        for step,part in zip(steps,parts):
            step.grant_id=enc.grant_id;step.amount_cents=part
    return steps

def fund(db,id,data,user):
    plan=lock_treatment(db,id)
    folio=get(db,Accession,plan.accession_id,True)
    grant=get(db,Grant,data.grant_id,True)
    reject(plan.status!='draft','仅草案可占用经费')
    reject(not reviewed_risk(db,folio.id),'最新病害分级未复核，不能开工')
    reject(not db.scalar(select(Isolation).where(Isolation.accession_id==folio.id,Isolation.active==True)),'卷册未在柜')
    reject(grant.budget_cents-grant.spent_cents-grant.reserved_cents<plan.estimated_cents,'专项余额不足；修订估价或改选专项后重试')
    grant.reserved_cents+=plan.estimated_cents;plan.status='funded';folio.status='repair'
    row=Encumbrance(treatment_id=id,grant_id=grant.id,amount_cents=plan.estimated_cents);db.add(row);db.flush()
    _assign_steps(db,id,grant.id,plan.estimated_cents)
    return finish(db,user,'专项经费占用',row)

def _prorate(actual,amounts):
    # 核销额按各笔占款比例分摊，分币余数优先给余数最大且仍有容量的那笔。
    total=sum(amounts)
    if actual>=total:return list(amounts)
    parts=[actual*amount//total for amount in amounts]
    remaining=actual-sum(parts)
    order=sorted(range(len(amounts)),key=lambda i:(-(actual*amounts[i]%total),-amounts[i],i))
    while remaining>0:
        progressed=False
        for i in order:
            if remaining==0:break
            if parts[i]<amounts[i]:
                parts[i]+=1;remaining-=1;progressed=True
        if not progressed:break
    return parts

def settle(db,id,data,user):
    plan=lock_treatment(db,id)
    reject(plan.status!='funded','方案尚未占款或已核销')
    reject(db.scalar(select(Step).where(Step.treatment_id==id,Step.done==False)) is not None,'全部依赖工序签证后才能核销')
    encs=list(db.scalars(select(Encumbrance).where(Encumbrance.treatment_id==id,Encumbrance.status=='held').order_by(Encumbrance.id)))
    reject(not encs,'未找到有效占款')
    reserved=sum(enc.amount_cents for enc in encs)
    reject(data.actual_cents>reserved,'核销额超过占款，需先办理追加方案')
    grants={}
    for grant_id in sorted({enc.grant_id for enc in encs}):
        grants[grant_id]=get(db,Grant,grant_id,True)
    parts=_prorate(data.actual_cents,[enc.amount_cents for enc in encs])
    for enc,part in zip(encs,parts):
        grant=grants[enc.grant_id]
        grant.reserved_cents-=enc.amount_cents;grant.spent_cents+=part
        enc.status='settled';enc.actual_cents=part
    plan.status='completed'
    audit(db,user,'经费核销',f'方案#{id} 实际{data.actual_cents}分 占款{reserved}分')
    db.commit()
    result={**dump(encs[0]),'actual_cents':data.actual_cents,'encumbrances':[dump(enc) for enc in encs]}
    return result

def _migration_key(treatment_id,source_grant_id,target_grant_id,step_ids,expected_signed,encumbrance_ids):
    # 自动幂等键只看迁移意图（方案、目标专项、所选工序、当时已签证工序与有效占款行），
    # 不含原专项，自动识别原专项的重试仍命中同一存根；撤销重占后占款行变化，可再次迁移。
    raw=f'{treatment_id}|{source_grant_id or "auto"}|{target_grant_id}|{sorted(step_ids)}|{sorted(expected_signed)}|{sorted(encumbrance_ids)}'
    return 'auto-'+hashlib.sha256(raw.encode()).hexdigest()[:96]

def _prior(db,key,treatment_id):
    prior=db.scalar(select(EncumbranceTransfer).where(EncumbranceTransfer.idempotency_key==key))
    if not prior:return None
    reject(prior.treatment_id!=treatment_id,'幂等键已被其他方案使用，请更换后重试')
    result=dump(prior);result['duplicate']=True;return result

def _migrate_once(db,id,data,user,requested,expected):
    plan=lock_treatment(db,id)
    reject(plan.status!='funded','仅已占款施工中的方案可迁移未施工工序')
    reject(data.target_grant_id==data.source_grant_id,'目标专项须与原专项不同')
    held=list(db.scalars(select(Encumbrance).where(Encumbrance.treatment_id==id,Encumbrance.status=='held').order_by(Encumbrance.id)))
    reject(not held,'该方案没有有效占款可迁移')
    for enc in held:_ensure_step_funding(db,id,enc)
    steps=_steps(db,id)
    signed={s.id for s in steps if s.done}
    if data.expected_signed_step_ids is not None:
        reject(expected!=signed,'迁移期间工序签证状态已变化，整次迁移未生效，请刷新后重试')
    key=data.idempotency_key or _migration_key(id,data.source_grant_id,data.target_grant_id,requested,signed,[enc.id for enc in held])
    prior=_prior(db,key,id)
    if prior:return prior
    if data.source_grant_id is not None:
        source=next((enc for enc in held if enc.grant_id==data.source_grant_id),None)
        reject(source is None,'原专项未持有该方案的有效占款，请刷新后重试')
    else:
        candidate_grants={s.grant_id for s in steps if s.id in requested} if requested else {s.grant_id for s in steps if not s.done}
        reject(len(candidate_grants)!=1,'未施工工序分属多个专项，请指定原专项后迁移')
        grant_id=next(iter(candidate_grants))
        source=next((enc for enc in held if enc.grant_id==grant_id),None)
        reject(source is None,'所选工序当前承款专项没有有效占款，请刷新后重试')
        reject(data.target_grant_id==grant_id,'所选工序已由目标专项承款，无需迁移')
    by_id={s.id:s for s in steps}
    move_ids=requested or sorted(s.id for s in steps if not s.done and s.grant_id==source.grant_id)
    reject(not move_ids,'该方案已无未施工工序可迁移')
    moved=[]
    for step_id in move_ids:
        step=by_id.get(step_id)
        reject(step is None,'所选工序不属于该方案')
        reject(step.done,'所选工序已签证，不能迁移；已签证工序仍归原专项')
        reject(step.grant_id!=source.grant_id,'所选工序不在原专项占款范围内')
        moved.append(step)
    amount=sum(s.amount_cents for s in moved)
    reject(amount<=0,'所选工序在原专项已无未占款金额，无需迁移')
    reject(amount>source.amount_cents,'迁移额超过原专项该方案占款，整次迁移未生效')
    grants={}
    for grant_id in sorted({source.grant_id,data.target_grant_id}):
        grants[grant_id]=get(db,Grant,grant_id,True)
    src=grants[source.grant_id];dst=grants[data.target_grant_id]
    src_available_before=src.budget_cents-src.spent_cents-src.reserved_cents
    dst_available_before=dst.budget_cents-dst.spent_cents-dst.reserved_cents
    reject(dst_available_before<amount,'目标专项余额不足，整次迁移未生效')
    retained=source.amount_cents-amount
    original=source.amount_cents
    source.amount_cents=retained
    if retained<=0:source.status='transferred'
    target=db.scalar(select(Encumbrance).where(Encumbrance.treatment_id==id,Encumbrance.grant_id==dst.id,Encumbrance.status=='held'))
    if target:
        target.amount_cents+=amount
    else:
        target=Encumbrance(treatment_id=id,grant_id=dst.id,amount_cents=amount,status='held');db.add(target);db.flush()
    for step in moved:
        step.grant_id=dst.id
    src.reserved_cents-=amount;dst.reserved_cents+=amount
    moved_positions=[s.position for s in moved]
    row=EncumbranceTransfer(
        treatment_id=id,source_grant_id=src.id,target_grant_id=dst.id,
        source_encumbrance_id=source.id,target_encumbrance_id=target.id,
        amount_cents=amount,retained_cents=retained,original_amount_cents=original,
        signed_step_ids=sorted(signed),migrated_positions=moved_positions,
        retained_positions=sorted(s.position for s in steps if s.id not in {m.id for m in moved}),
        source_reserved_before=src.reserved_cents+amount,source_reserved_after=src.reserved_cents,
        target_reserved_before=dst.reserved_cents-amount,target_reserved_after=dst.reserved_cents,
        source_available_before=src_available_before,
        source_available_after=src.budget_cents-src.spent_cents-src.reserved_cents,
        target_available_before=dst_available_before,
        target_available_after=dst.budget_cents-dst.spent_cents-dst.reserved_cents,
        idempotency_key=key,actor=user.username)
    db.add(row);db.flush()
    audit(db,user,'未施工工序迁移占款',f'方案#{id} 专项#{src.id}→#{dst.id} {amount}分 保留{retained}分')
    db.commit()
    result=dump(row);result['duplicate']=False;return result

def migrate(db,id,data,user):
    """把方案中尚未签证工序的占款从原专项一次性迁至目标专项。

    已签证工序继续留在原专项，已核销记录不动；两专项余额在同一事务内切换。
    请求带幂等键（未带时按方案、目标专项与工序内容自动生成），重复提交返回原存根，不再生成占款。
    """
    requested=sorted(set(data.step_ids or []))
    expected=set(data.expected_signed_step_ids or [])
    if data.idempotency_key:
        prior=_prior(db,data.idempotency_key,id)
        if prior:return prior
    try:
        return _migrate_once(db,id,data,user,requested,expected)
    except Exception:
        # 任一校验或约束失败都回滚，保证原专项与目标专项余额不出现半迁移。
        db.rollback();raise
