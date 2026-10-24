# Search-engine feature comparison

This is a feature-oriented comparison of documented capabilities, not a controlled latency or relevance benchmark.

| Engine | Main strengths | OpenFind takeaway |
|---|---|---|
| Google Search | Broad index, autocomplete, featured answer formats and category filters | Keep search UI legible and suggestions helpful |
| DuckDuckGo | Privacy-oriented positioning, Instant Answers and bang shortcuts | Include a direct-answer card and explain upstream data flow |
| Microsoft Bing | Web, news, images and video verticals, advanced query operators | Add category tabs and safe-search selection |
| Brave Search | Independent index and ranking customization via Goggles | Favor source transparency and user control |
| SearXNG | Open-source, self-hostable metasearch across many sources | Aggregate results without a user profile or saved search history |

Sources:
- Google autocomplete: https://support.google.com/websearch/answer/7368877
- Google search filters: https://support.google.com/websearch/answer/2466433
- DuckDuckGo Instant Answers: https://duckduckgo.com/duckduckgo-help-pages/features/instant-answers-and-other-features
- Bing advanced search: https://support.microsoft.com/en-us/bing/advanced-search-options
- Brave Search: https://brave.com/search/
- SearXNG overview: https://docs.searxng.org/user/about.html

## What was measured

No controlled live speed, relevance, or load test is claimed. A fair ranking would require a fixed query set, same geographic region, repeated requests, consistent network conditions, and provider-result scoring. This project takes a qualitative feature benchmark approach instead.

## Selected design

OpenFind includes a responsive interface, locally generated debounced suggestions, web/news/images/videos categories, an arithmetic instant-answer card, paginated results, dark/light mode, safe-search control, and JSON export. It avoids accounts, query-history persistence, analytics SDKs, paid search API keys, and background services.
