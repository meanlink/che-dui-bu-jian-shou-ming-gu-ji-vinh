import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const stations={
 board:()=>api('/stations/board'),
 create:data=>api('/stations',{method:'POST',body:JSON.stringify(data)}),
 detail:id=>api('/stations/'+id),
 publish:(id,job_id)=>api('/stations/'+id+'/publish',{method:'POST',body:JSON.stringify({job_id})}),
 revoke:(id,version)=>api('/stations/'+id+'/revoke',{method:'POST',body:JSON.stringify({version})}),
 reconcile:id=>api('/stations/'+id+'/reconcile',{method:'POST'}),
 reconcileAll:()=>api('/stations/reconcile',{method:'POST'}),
 retry:(id,directive)=>api('/stations/'+id+'/directives/'+directive+'/retry',{method:'POST'}),
 stationSide:id=>api('/stations/'+id+'/station'),
 drill:(id,flags)=>api('/stations/'+id+'/station/drill',{method:'POST',body:JSON.stringify(flags)}),
 release:(id,order)=>api('/stations/'+id+'/station/release',{method:'POST',body:JSON.stringify({order})}),
 receipt:(id,directive)=>api('/stations/'+id+'/station/directives/'+directive),
};
