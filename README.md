# knowhub
Enterprise Knowledge Hub (in short, knowhub) is a central knowledge repository system for an enterprise. It stores and provides operational and business information from different enterprise systems along with relationship between them. Currently, it extracts information from local files/folders, JIRA Issues, Confluence Pages, ServiceNow Incidents/Problems/CRs and Salesforce objects (Opportunities, Products, Accounts etc), identifies relationships between these information and then stores them. The repository remains in-sync with these enterprise systems as and when any information is added/modified/deleted therein.

After retrieving the data from enterprise systems, the hub scrubs the data to anonymize confidential data such as PII data, customer information. You can also extend this easily to implement any specific data privacy pertaining to your organization or use case.


