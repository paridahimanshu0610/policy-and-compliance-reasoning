# policy-and-compliance-reasoning
### Relevant resources:
#### Main discussion thread:
https://chatgpt.com/c/6980fea9-d24c-8328-aea1-837b5c1cb301

#### FINRA discussion threads:

https://chatgpt.com/c/69940fb9-5e28-8325-bf46-5f40c0dbcead

https://gemini.google.com/u/2/app/62b8a627be5911ca?is_sa=1&is_sa=1&android-min-version=301356232&ios-min-version=322.0&campaign_id=bkws&utm_source=sem&utm_medium=paid-media&utm_campaign=bkws&pt=9008&mt=8&ct=p-growth-sem-bkws&gclsrc=aw.ds&gad_source=1&gad_campaignid=22908443171&gbraid=0AAAAApk5Bhn586sDtpOAaLTzWP3ab9Mik&gclid=CjwKCAiAssfLBhBDEiwAcLpwfkxga6rXYgxFYnFmmvtxIoGVryS1vH6eo0BVRkcT3skSASiRbKbHcBoCz6sQAvD_BwE&pageId=none


### Step Guidance Resources:

#### 1. How do we prepare semantic structured text from the PDF?

https://chatgpt.com/s/t_69af63798b288191b19358c5b7446a5e


#### 2. Ways in which the LLM reasoning can be done

https://chatgpt.com/s/t_69afac9a8f94819184947260da7ec86e 

#### 3. Difference between RAG and current system

https://chatgpt.com/s/t_69afb57624d8819190485618d6df0299 

#### 4. If structure + reasoning is doing most of the heavy lifting… what exactly is MCP adding?

https://chatgpt.com/s/t_69afb98a6de48191a7b7f7783cab51a6 

MCP "standardizes" how agents discover and invoke capabilities. MCP solves this problem: How to make a structured reasoning engine reusable, modular, and agent-interoperable?

#### 5. How exactly retrieval in MCP server differs from RAG-based retrieval?

- Execution is function-based, but selection of which resource to invoke is reasoning-driven. The agent interprets the user intent and dynamically chooses relevant capabilities exposed via MCP. 
- In RAG, we retrieve by similarity and interpret everything afterward.

```
Query → embedding → top-k similar chunks
```

- In MCP, we retrieve by capability (exposed resources), apply constraints deliberately, and reason with structured facts.

```
Query → intent parsing → choose capabilities (choose the from the exposed resources) → structured calls (actual call to those resource functions)
```

https://chatgpt.com/s/t_69afbe59dde08191a19ead76404cee65


### Interview focused questions:

1. Simulate an interviewer trying to poke holes in your MCP usage