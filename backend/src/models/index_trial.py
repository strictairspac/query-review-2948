from datetime import datetime
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,Text,ForeignKey,DateTime,func,JSON,Index,text
from ..config.database import Base


class IndexTrial(Base):
    """一次普通组合索引试验：待建 → 建索引 → 验证 → 保留或清理。"""

    __tablename__='index_trials'
    __table_args__=(
        Index(
            'ux_index_trials_active_group',
            'column_group',
            unique=True,
            postgresql_where=text("status IN ('pending','building','verifying','verified','retained')"),
        ),
    )
    id:Mapped[int]=mapped_column(primary_key=True)
    column_group:Mapped[str]=mapped_column(String(200),nullable=False)
    columns:Mapped[list]=mapped_column(JSON,nullable=False)
    index_name:Mapped[str]=mapped_column(String(120),nullable=False)
    sample_id:Mapped[int]=mapped_column(ForeignKey('samples.id'))
    status:Mapped[str]=mapped_column(String(20),default='pending')
    owner_token:Mapped[str]=mapped_column(String(40),nullable=False)
    requested_by:Mapped[str]=mapped_column(String(120))
    requested_reason:Mapped[str]=mapped_column(Text,default='')
    plan_before:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    plan_after:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    comparison:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    evidence_sha256:Mapped[str|None]=mapped_column(String(64),nullable=True)
    decision:Mapped[str|None]=mapped_column(String(20),nullable=True)
    decision_reason:Mapped[str|None]=mapped_column(Text,nullable=True)
    decision_by:Mapped[str|None]=mapped_column(String(120),nullable=True)
    decision_evidence_sha256:Mapped[str|None]=mapped_column(String(64),nullable=True)
    decided_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    last_error:Mapped[str|None]=mapped_column(Text,nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    updated_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),onupdate=func.now())


class IndexTrialEvent(Base):
    """状态迁移与原因留痕，页面据此展示“为什么”。"""

    __tablename__='index_trial_events'
    id:Mapped[int]=mapped_column(primary_key=True)
    trial_id:Mapped[int]=mapped_column(ForeignKey('index_trials.id'),index=True)
    from_status:Mapped[str|None]=mapped_column(String(20),nullable=True)
    to_status:Mapped[str|None]=mapped_column(String(20),nullable=True)
    reason:Mapped[str]=mapped_column(Text)
    actor:Mapped[str]=mapped_column(String(120))
    detail:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
