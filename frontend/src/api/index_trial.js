import{api,post}from'./client.js';
export const listTrials=()=>api('/index-trials');
export const getTrial=id=>api('/index-trials/'+id);
export const createTrial=data=>post('/index-trials',data);
export const decideTrial=(id,data)=>post('/index-trials/'+id+'/decision',data);
export const cleanupTrial=(id,data)=>post('/index-trials/'+id+'/cleanup',data);
export async function exportTrial(id){const d=await getTrial(id);const u=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download='index-trial-'+id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}
