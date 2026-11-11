# knowhub
Enterprise Knowledge Hub (in short, knowhub) is a central knowledge repository system for an enterprise. It stores and provides operational and business information from different enterprise systems along with relationship between them. Currently, it extracts information from local files/folders, JIRA Issues, Confluence Pages, ServiceNow Incidents/Problems/CRs and Salesforce objects (Opportunities, Products, Accounts etc), identifies relationships between these information and then stores them. The repository remains in-sync with these enterprise systems as and when any information is added/modified/deleted therein.

# Problem it solves
In an enterprise, information is scattered across different systems, be it files, emails, Confluence, JIRA, ServiceNow, ERPs, Salesforce etc. For both business services and business operations, it causes challenges since data is distributed. And even if you index this data in a central hub, you still need to join the dots between these data to make meaning out of them. For example, a ServiceNow Incident might refer to a Confluence Page which in-turn refers to a JIRA Issue. So, when you (or an AI Agent) are looking for a 360-degree view of a ServiceNow Incident, you need all the related data from other systems as well. 

As more information/data gets added/updated in different enterprise systems, your central knowledge hub must also get refreshed so that it remains in-sync.

knowhub solves both above problems. 

# How about data security and data privacy
After retrieving the data from enterprise systems, the knowhub scrubs the data to anonymize confidential data such as PII data, customer information. You can also extend this easily to implement any specific data privacy pertaining to your organization or use case.

# How to Install and Run

## Pre-requisites
1. Python version 3.11 or later.
2. Access to different Enterprise Systems (such as Salesforce, ServiceNow etc). The details are provided in sections below.
3. Access to LLM Models (for Inferencing and for Vector Embedding). The details are provided in sections below.

## Setup
1. Optional Step -> Create a Python Virtual Environment (python -m venv venv)
2. Clone this repository (git clone https://github.com/newtechcollab/knowhub.git)
3. Install the required Python libraries (python -m pip install - requirements.txt)

## Configuration
knowhub uses different environment variables - you can either set these in .env file (located in root folder) or set within your OS.

### LLM Configuration
  a. If you are using OpenAI APIs, then, set your OpenAI API Key in OPENAI_API_KEY. Also, set LLM_MODEL and EMBEDDING_MODEL accordingly.
  b. If you have Github Copilot subscription, you can use the underlying LLM of Github Copilot as the Inferencing Engine. However, Github Copilot does not provide access to any Vector Embedding model. I have built a utility that internally uses LLM of Github Copilot and open source Ollama hosted Embedding model. Check the utility module in this repository.

By default, knowhub assumes that you will use option 1a. In case you go for 1b or any other mechanism for LLM access, then, edit main.py and set openai_client member variable accordingly.



