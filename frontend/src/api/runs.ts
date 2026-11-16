import {request,post} from './client';import type {Run,GenerationSummary,GenerationDetail,GenerationDiff} from '../types/domain';
export const getRuns=()=>request<Run[]>('/runs');
export const createRun=(data:unknown)=>post<Run>('/runs',data);
export const getGenerations=(runId:number)=>request<GenerationSummary[]>('/runs/'+runId+'/generations');
export const getGeneration=(runId:number,generationId:number)=>request<GenerationDetail>('/runs/'+runId+'/generations/'+generationId);
export const createGeneration=(runId:number,data:unknown)=>post<{generation_id:number;generation_no:number;status:string}>('/runs/'+runId+'/generations',data);
export const retryGeneration=(runId:number,generationId:number)=>post<{generation_id:number;status:string;attempt:number}>('/runs/'+runId+'/generations/'+generationId+'/retry',{});
export const publishGeneration=(runId:number,generationId:number)=>post<{published:boolean}>('/runs/'+runId+'/generations/'+generationId+'/publish',{});
export const getDiff=(runId:number,fromId?:number,toId?:number)=>{const q=[fromId?`from_id=${fromId}`:'',toId?`to_id=${toId}`:''].filter(Boolean).join('&');return request<GenerationDiff>('/runs/'+runId+'/diff'+(q?'?'+q:''));};
