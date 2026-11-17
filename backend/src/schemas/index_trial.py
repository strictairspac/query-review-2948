from typing import Literal
from pydantic import BaseModel,Field


class IndexTrialInput(BaseModel):
    columns:list[str]=Field(min_length=2,max_length=4)
    sample_id:int=Field(gt=0)
    reason:str=Field(default='',max_length=500)


class IndexTrialDecision(BaseModel):
    decision:Literal['retain','cleanup']
    reason:str=Field(min_length=2,max_length=500)
    # 发布决定时必须与本次验证证据摘要一致，避免“决定”和“依据”分叉。
    evidence_sha256:str|None=Field(default=None,max_length=64)


class IndexTrialCleanup(BaseModel):
    reason:str=Field(default='人工清理失败残留',min_length=2,max_length=500)
