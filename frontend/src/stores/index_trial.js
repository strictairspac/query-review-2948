import{listTrials}from'../api/index_trial.js';export const indexTrials={items:[],async load(){this.items=await listTrials();return this.items}};
