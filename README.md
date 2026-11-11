# knowhub
Enterprise Knowledge Hub (in short, knowhub) is a central knowledge repository system for an enterprise. It stores and provides operational and business information from different enterprise systems along with relationship between them. Currently, it extracts information from local files/folders, JIRA Issues, Confluence Pages, ServiceNow Incidents/Problems/CRs and Salesforce objects (Opportunities, Products, Accounts etc), identifies relationships between these information and then stores them. The repository remains in-sync with these enterprise systems as and when any information is added/modified/deleted therein.

# Problem it solves
In an enterprise, information is scattered across different systems, be it files, emails, Confluence, JIRA, ServiceNow, ERPs, Salesforce etc. For both business services and business operations, it causes challenges since data is distributed. And even if you index this data in a central hub, you still need to join the dots between these data to make meaning out of them. For example, a ServiceNow Incident might refer to a Confluence Page which in-turn refers to a JIRA Issue. So, when you (or an AI Agent) are looking for a 360-degree view of a ServiceNow Incident, you need all the related data from other systems as well. 

As more information/data gets added/updated in different enterprise systems, your central knowledge hub must also get refreshed so that it remains in-sync.

knowhub solves both above problems. 

# How about data security and data privacy
After retrieving the data from enterprise systems, the knowhub scrubs the data to anonymize confidential data such as PII data, customer information. You can also extend this easily to implement any specific data privacy pertaining to your organization or use case.


