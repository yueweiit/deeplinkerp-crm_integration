const assert = require("node:assert/strict");
const test = require("node:test");
const finance=require("./public/js/finance_release.js");

test("only explicitly eligible orders reach the manual confirmation",()=>{
 assert.deepEqual(finance.eligibleNames([{name:"A",can_release:true},{name:"B",can_release:false},{name:"C"}]),["A"]);
});
test("partial batches retry only failed orders, never pending or uncertain responses",()=>{
 assert.deepEqual(finance.retryNames([{name:"A",state:"processing"},{name:"B",state:"failed"},{name:"C",state:"unknown"}]),["B"]);
});
test("processing and successful synchronization have distinct labels",()=>{
 assert.match(finance.stateLabel({process_status:"Deposit Confirmation Processing"}),/处理中/);
 assert.match(finance.stateLabel({process_status:"Pending Production"}),/已完成/);
 assert.equal(finance.stateLabel({process_status:"Rejected",last_confirmation:{status:"Failed"}}),"Rejected");
});
test("each receipt shows its own allocation and account currency, including drafts and cancellations",()=>{
 const html=finance.rowsHTML([{name:"SO-1",can_release:true,receipts:[{name:"PE-1",docstatus:0,payment_type:"Receive",allocated_amount:12,currency:"MXN"},{name:"PE-2",docstatus:2,payment_type:"Receive",allocated_amount:1,currency:"USD"}]}]);
 assert.match(html,/12\.00 MXN/); assert.match(html,/1\.00 USD/); assert.match(html,/草稿/); assert.match(html,/已取消/);
});
test("malicious source names cannot inject markup into financial records",()=>{
 const html=finance.rowsHTML([{name:'<script>x</script>',reason:'<img onerror=x>',can_release:false,can_reject:true}]);
 assert.ok(!html.includes('<script>')); assert.match(html,/&lt;img/);
 assert.match(html,/data-name="&lt;script&gt;x&lt;\/script&gt;"/);
});
test("reject is shown per order only when the server grants that action",()=>{
 const eligible=finance.rowsHTML([{name:"SO-1",can_reject:true}]);
 const blocked=finance.rowsHTML([{name:"SO-2",can_reject:false}]);
 assert.match(eligible,/crm-release-reject/);
 assert.match(eligible,/财务驳回/);
 assert.match(eligible,/data-name="SO-1"/);
 assert.ok(!blocked.includes("crm-release-reject"));
});
test("button wording identifies production and displays the selected count",()=>{
 assert.equal(finance.buttonLabel(1),"允许生产（1单）"); assert.equal(finance.buttonLabel(5),"允许生产（5单）");
});

test("current synchronized status wins over an older failed attempt",()=>{
 assert.match(finance.stateLabel({process_status:"Pending Production",last_confirmation:{status:"Failed"}}),/已完成/);
});

test("failed MES response shows the safe summary and escapes it in the review drawer",()=>{
 const row={name:"SO-1",process_status:"Pending Deposit Confirmation",last_confirmation:{status:"Failed"},sync_error:{code:"ERP_SALES_ORDER_IDENTITY_CONFLICT",message:"MES 订单映射冲突，请核对 ERP/CRM 单号与 MES 既有映射，处理冲突后重试。"}};
 assert.match(finance.stateLabel(row),/订单映射冲突/);
 assert.match(finance.rowsHTML([{...row,sync_error:{message:"<script>x</script>"}}]),/&lt;script&gt;x&lt;\/script&gt;/);
});

test("release operator without log read permission sees failure and can retry",()=>{
 const row={name:"SO-1",process_status:"Pending Deposit Confirmation",can_release:true,last_confirmation:null,sync_status:"Failed",sync_error:null};
 assert.equal(finance.failedReview(row),true);
 assert.match(finance.stateLabel(row),/同步失败/);
 const results=[{name:row.name,state:"processing"}];
 finance.reconcileResults(results,[row]);
 assert.deepEqual(finance.retryNames(results),["SO-1"]);
 assert.equal(finance.failedReview({...row,state:undefined,process_status:"Deposit Confirmation Processing",sync_status:"Pending"}),false);
 const stillProcessing={name:"SO-2",process_status:"Deposit Confirmation Processing",sync_status:"Failed"};
 const inFlight=[{name:"SO-2",state:"processing"}];
 finance.reconcileResults(inFlight,[stillProcessing]);
 assert.equal(inFlight[0].state,"processing");
});
