I need to further refine my FastAPI backend and React dashboard (mulepredator_dashboard.html). Please update the code with the following specific requirements:
1. Two-Tier Filtering System (Transaction Queue)
Primary Filter (Top row, 3 buttons):
'All': Shows absolutely every transaction in the live feed (including clean/benign ones).
'Suspicious': Shows transactions flagged by exactly 1 engine (single signal).
'Flagged': Shows transactions flagged by 2 or more engines (converged alerts).
Secondary Filter (Bottom row, keep existing):
Keep the current sub-filters: 'All', 'High', 'Inv', 'Mon'. These should filter whatever is currently selected in the Primary Filter based on their tier.

2. Dynamic Network & Mule Cluster View (Crucial)
The current 'Network View' SVG is static. I need it to reflect the actual transaction data, especially for 'high fan-in' (smurfing) alerts.
Backend update: When the Graph Engine flags a transaction, the API should include a cluster_details object (or similar) in the response containing the actual account_id of the collector (hub) and the account_ids of the senders (spokes) that sent money in.
Frontend update: Remove the static mock ring. If the selected transaction has graph/network risk, display the actual sender account IDs and the collector account ID in the UI. You can render this as a dynamic list or a data table next to/below the graph SVG so the analyst sees exactly who is sending money to whom.
4. Implementation Rules
Create a /feed endpoint on the backend that stores a rolling window of the last 100 transactions (clean and flagged) so the dashboard has data to pull.
Ensure the dashboard's USE_LIVE_API = true polling fetches from this new /feed endpoint.