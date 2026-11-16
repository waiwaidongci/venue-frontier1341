from pydantic import BaseModel,Field
class CaseInput(BaseModel):
    title:str=Field(min_length=2,max_length=160)
    run_id:int
    observation_id:int
    generation_id:int|None=None
    hypothesis:str=Field(min_length=10,max_length=1200)
    verification:str=Field(min_length=10,max_length=1000)
class CaseMigrationInput(BaseModel):
    generation_id:int
