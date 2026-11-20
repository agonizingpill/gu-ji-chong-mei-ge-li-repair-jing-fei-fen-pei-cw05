from sqlalchemy import inspect,select,text
from .database import Session
from .models import Treatment,Encumbrance,Step
from .grant_service import _split

def ensure_schema(engine):
    """create_all 只补建缺失表；旧库升级时给既有工序补上承款明细列。"""
    with engine.begin() as conn:
        inspector=inspect(conn)
        if 'steps' not in inspector.get_table_names():return
        columns={c['name'] for c in inspector.get_columns('steps')}
        if 'grant_id' not in columns:
            conn.execute(text('ALTER TABLE steps ADD COLUMN grant_id INTEGER REFERENCES grants(id)'))
        if 'amount_cents' not in columns:
            conn.execute(text('ALTER TABLE steps ADD COLUMN amount_cents INTEGER NOT NULL DEFAULT 0'))

def backfill_step_funding():
    """升级前已占款的方案没有逐道承款信息，按唯一有效占款额补记，来源存根不受影响。"""
    with Session() as db:
        for plan in db.scalars(select(Treatment).where(Treatment.status.in_(['funded','completed']))):
            steps=list(db.scalars(select(Step).where(Step.treatment_id==plan.id).order_by(Step.position,Step.id)))
            if not steps or any(step.grant_id is not None for step in steps):continue
            encs=list(db.scalars(select(Encumbrance).where(Encumbrance.treatment_id==plan.id,Encumbrance.status.in_(['held','settled'])).order_by(Encumbrance.id)))
            if len(encs)!=1:continue
            enc=encs[0]
            for step,part in zip(steps,_split(enc.amount_cents,len(steps))):
                step.grant_id=enc.grant_id;step.amount_cents=part
        db.commit()
