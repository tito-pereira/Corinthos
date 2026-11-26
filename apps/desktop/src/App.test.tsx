// SPDX-License-Identifier: MPL-2.0
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
const { invoke, listen } = vi.hoisted(() => ({invoke: vi.fn(), listen: vi.fn()}));
vi.mock("@tauri-apps/api/core", () => ({invoke}));
vi.mock("@tauri-apps/api/event", () => ({listen}));
const project = {root:"C:/Fixture",project_id:"fixture",workspace_format:1,name:"Fixture",description:"Description"};
let host: HTMLDivElement, root: Root;
const environment = globalThis as typeof globalThis & {IS_REACT_ACT_ENVIRONMENT:boolean};
beforeEach(() => {
  environment.IS_REACT_ACT_ENVIRONMENT=true;
  vi.resetAllMocks();listen.mockResolvedValue(()=>undefined);
  invoke.mockImplementation(async(command:string,args?:{method:string;params:Record<string,unknown>})=>{
    if(command==="backend_snapshot")return{connection:"ready",desktop:{version:"0.1.0",identifier:"fixture"},handshake:{protocol_version:2,version:"0.1.0",methods:["gui.projects","update.status"]},providers:{providers:[{provider:"local",command:[],available:true}]}};
    if(command==="select_project_directory")return"C:/Fixture/repo";
    const value=args?.method==="gui.projects"?{base:"C:/Projects",projects:Array.from({length:12},(_,index)=>({...project,root:"C:/Project"+index,project_id:"id"+index,name:"Project"+index,available:true}))}
      :args?.method==="gui.open"?project
      :args?.method==="gui.conversation.open"?{conversation_id:args.params.project?"project-conversation":"main-conversation",kind:args.params.project?"project":"main",project:args.params.project,messages:[],omitted:0}
      :args?.method==="gui.runs"?{runs:[]}
      :args?.method==="gui.summary"?{kind:args.params.kind,empty:true,items:[]}
      :args?.method==="gui.execute"?{empty:true,message:"No Tasks Yet"}
      :args?.method==="update.status"?{phase:"UNSUPPORTED",installed_version:"0.1.0",candidate_version:null,message:"Source preview",progress:0,error:null,can_start:false,can_finish:false}:{};
    return args?.method.startsWith("gui.")?{ok:true,value}:value;
  });
  host=document.createElement("div");document.body.append(host);root=createRoot(host);
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();environment.IS_REACT_ACT_ENVIRONMENT=false;});
async function render(){await act(async()=>{root.render(<App/>);await new Promise(resolve=>setTimeout(resolve,0));});}
function button(text:string){const found=Array.from(host.querySelectorAll("button")).find(node=>node.textContent?.trim()===text);if(!found)throw new Error("Missing "+text);return found;}
async function click(text:string){await act(async()=>{button(text).click();await new Promise(resolve=>setTimeout(resolve,0));});}
describe("two-page GUI",()=>{
  it("opens Main without project work and anchors Update",async()=>{
    await render();expect(host.textContent).toContain("Welcome to Corinthos");expect(host.querySelector(".sidebar footer")?.textContent).toContain("Update");
    expect(invoke).toHaveBeenCalledWith("workspace_request",{method:"gui.conversation.open",params:{project:null}});
    expect(invoke.mock.calls.some(call=>["skill.run","gui.execute","gui.create"].includes(call[1]?.method))).toBe(false);
  });
  it("has exactly two creation fields and a passive cancel",async()=>{
    await render();await click("Create Project");expect(host.querySelector("[role=dialog]")?.querySelectorAll("input,textarea")).toHaveLength(2);await click("Cancel");
    expect(host.querySelector("[role=dialog]")).toBeNull();expect(invoke.mock.calls.some(call=>call[1]?.method==="gui.create")).toBe(false);
  });
  it("caps recents at ten plus Choose Other and opens the containing project",async()=>{
    await render();await click("Resume Project");expect(host.querySelectorAll(".recents button")).toHaveLength(11);await click("... Choose Other");
    expect(invoke).toHaveBeenCalledWith("select_project_directory");expect(invoke).toHaveBeenCalledWith("workspace_request",{method:"gui.open",params:{path:"C:/Fixture/repo"}});
    expect(host.textContent).toContain("This is the Fixture project page");
  });
  it("reads summaries without dispatch and uses exact empty/task states",async()=>{
    await render();await click("Resume Project");await click("... Choose Other");await click("Idea List");expect(host.querySelector("[role=dialog]")?.textContent).toContain("Nothing Yet");
    await act(async()=>host.querySelector<HTMLButtonElement>("[aria-label='Close dialog']")!.click());await click("Task List");expect(host.querySelector("[role=dialog]")?.textContent).toContain("Nothing Yet");
    await act(async()=>host.querySelector<HTMLButtonElement>("[aria-label='Close dialog']")!.click());
    expect(invoke.mock.calls.filter(call=>call[1]?.method==="gui.summary")).toHaveLength(2);expect(invoke.mock.calls.some(call=>call[1]?.method==="gui.execute")).toBe(false);
    await click("Execute Tasks");expect(host.textContent).toContain("No Tasks Yet");
  });
  it("retires discarded runs without polling removed artifacts",async()=>{
    const normalInvoke=invoke.getMockImplementation()!;
    let discarded=false;
    const message={role:"assistant",text:"Retained conversation reply",run_id:"run-one",status:"SUCCEEDED"};
    invoke.mockImplementation(async(command:string,args?:{method:string;params:Record<string,unknown>})=>{
      if(command!=="workspace_request")return normalInvoke(command,args);
      let value;
      if(args?.method==="gui.conversation.open"&&args.params.project)value={conversation_id:"project-conversation",kind:"project",project,messages:[message],omitted:0};
      else if(args?.method==="gui.runs")value={runs:[{run_id:"run-one",skill:"conversation",status:"SUCCEEDED",integrated:false,error:null}]};
      else if(args?.method==="gui.conversation.output"){
        if(discarded)throw new Error("Stale output request after discard");
        value={text:message.text,cursor:message.text.length,complete:true};
      }else if(args?.method==="gui.run.action")value={operation_id:"discard-op",kind:"action",status:"RUNNING",phase:"Discarding",project,run:{run_id:"run-one",project_id:project.project_id}};
      else if(args?.method==="gui.operation.get"){
        discarded=true;
        value={operation_id:"discard-op",kind:"action",status:"SUCCEEDED",phase:"Done",project,run:{run_id:"run-one",project_id:project.project_id},result:{discarded:"run-one"}};
      }else if(args?.method==="gui.conversation.get")value={conversation_id:"project-conversation",kind:"project",project,messages:[{role:"assistant",text:message.text}],omitted:0};
      else if(args?.method==="gui.run.get"){
        if(discarded)throw new Error("Stale run request after discard");
        value={run_id:"run-one",status:"SUCCEEDED",integrated:false,error:null};
      }else return normalInvoke(command,args);
      return{ok:true,value};
    });
    await render();await click("Resume Project");await click("... Choose Other");
    await click("Discard");
    await act(async()=>{host.querySelector<HTMLFormElement>("[role=dialog] form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));});
    await act(async()=>{await new Promise(resolve=>setTimeout(resolve,500));});
    expect(discarded).toBe(true);expect(host.querySelector(".run-card")).toBeNull();
    const retiredRequests=invoke.mock.calls.filter(call=>["gui.conversation.output","gui.run.get"].includes(call[1]?.method)).length;
    await act(async()=>{await new Promise(resolve=>setTimeout(resolve,1000));});
    expect(invoke.mock.calls.filter(call=>["gui.conversation.output","gui.run.get"].includes(call[1]?.method))).toHaveLength(retiredRequests);
    expect(host.textContent).toContain(message.text);expect(host.textContent).not.toContain("Stale");
  });
  it.each([false,true])("merges a delayed final chunk into latest terminal state (switch page: %s)",async(switchPage)=>{
    const normalInvoke=invoke.getMockImplementation()!;
    const initialMessages=[{role:"user",text:"Original question"},{role:"assistant",text:"",operation_id:"conversation-op",run_id:"run-one",status:"RUNNING"}];
    const terminalMessages=[initialMessages[0],{...initialMessages[1],status:"SUCCEEDED",text:"finished reply"},{role:"user",text:"Retained newer message"}];
    let operationReads=0,outputRequested=false;
    let finishOutput!:(value:unknown)=>void;
    const output=new Promise(resolve=>{finishOutput=resolve;});
    invoke.mockImplementation(async(command:string,args?:{method:string;params:Record<string,unknown>})=>{
      if(command!=="workspace_request")return normalInvoke(command,args);
      let value;
      if(args?.method==="gui.conversation.open"&&args.params.project)value={conversation_id:"project-conversation",kind:"project",project,messages:operationReads>1?terminalMessages:initialMessages,omitted:0};
      else if(args?.method==="gui.operation.get")value={operation_id:"conversation-op",kind:"conversation",status:++operationReads===1?"RUNNING":"SUCCEEDED",phase:"Reply",project,run:{run_id:"run-one",project_id:project.project_id}};
      else if(args?.method==="gui.conversation.get")value={conversation_id:"project-conversation",kind:"project",project,messages:terminalMessages,omitted:0};
      else if(args?.method==="gui.conversation.output"){outputRequested=true;value=await output;}
      else if(args?.method==="gui.run.get")value={run_id:"run-one",status:"SUCCEEDED",integrated:false,error:null};
      else return normalInvoke(command,args);
      return{ok:true,value};
    });
    await render();await click("Resume Project");await click("... Choose Other");
    expect(host.querySelector<HTMLTextAreaElement>("textarea")?.disabled).toBe(true);
    await act(async()=>{await new Promise(resolve=>setTimeout(resolve,500));});
    expect(outputRequested).toBe(true);
    if(switchPage)await act(async()=>{host.querySelector<HTMLButtonElement>("[aria-label='Main page']")!.click();});
    await act(async()=>{finishOutput({text:"finished reply",cursor:14,complete:true});await new Promise(resolve=>setTimeout(resolve,0));});
    if(switchPage){
      expect(host.textContent).toContain("Welcome to Corinthos");expect(host.textContent).not.toContain("finished reply");
      await click("Resume Project");await click("... Choose Other");
    }
    expect(host.querySelector<HTMLTextAreaElement>("textarea")?.disabled).toBe(false);
    expect(host.querySelector("[role=log]")?.textContent).toContain("SUCCEEDED");
    expect(host.querySelector("[role=log]")?.textContent).not.toContain("RUNNING");
    expect(host.querySelector("[role=log]")?.textContent).toContain("Retained newer message");
    expect(host.textContent?.match(/finished reply/g)).toHaveLength(1);
  });
});
