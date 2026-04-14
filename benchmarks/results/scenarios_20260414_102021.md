# codeclub benchmark results

*Generated 2026-04-14 10:20 UTC*

## Compression — token savings by file

| File | Lines | Language | Original | Stub | Full pipeline | Stub saved | Full saved |
|------|------:|----------|-------:|-----:|-------------:|----------:|----------:|
| wallet_local.py | 409 | python | 2,478 | 782 | 489 | 68% | 80% |
| wallet_stripe.py | 934 | python | 7,202 | 423 | 300 | 94% | 96% |
| wallet_bridge_snippet.py | 78 | python | 504 | 279 | 160 | 45% | 68% |
| wallet_provider_snippet.py | 163 | python | 1,067 | 996 | 632 | 7% | 41% |
| stripe_connect.jsx | 678 | javascript | 5,916 | 5,916 | 5,916 | 0% | 0% |
| [2 wallet files combined] | 1343 | python | 9,680 | 1,205 | 789 | 88% | 92% |

## Tiered generation — map+fill across backends

| Backend | Map model | Time | Tokens in | Tokens out | Cost | GPT-4o equiv | Savings | Quality |
|---------|-----------|-----:|----------:|-----------:|-----:|-------------:|--------:|--------:|
| B580-rnj1 | llama-server | 26.6s | 1,339 | 1,430 | $0.000166 | $0.017647 | 106× | 100% |
| OR-gemma4-moe | google/gemma-4-26b-a4b-it | 30.1s | 1,495 | 1,630 | $0.000128 | $0.020037 | 156× | 75% |
| OR-llama70b | meta-llama/llama-3.3-70b-instruct | 13.4s | 892 | 854 | $0.000058 | $0.010770 | 186× | 100% |
| B580-rnj1 | llama-server | 19.2s | 761 | 889 | $0.000120 | $0.010793 | 90× | 100% |
| OR-gemma4-moe | google/gemma-4-26b-a4b-it | 24.8s | 1,013 | 969 | $0.000128 | $0.012223 | 95× | 75% |
| OR-llama70b | meta-llama/llama-3.3-70b-instruct | 16.9s | 600 | 540 | $0.000056 | $0.006900 | 123× | 100% |
| B580-rnj1 | llama-server | 39.7s | 1,776 | 1,820 | $0.000248 | $0.022640 | 91× | 75% |
| OR-gemma4-moe | google/gemma-4-26b-a4b-it | 71.7s | 1,761 | 2,295 | $0.000166 | $0.027353 | 165× | 100% |
| OR-llama70b | meta-llama/llama-3.3-70b-instruct | 17.4s | 1,032 | 910 | $0.000056 | $0.011680 | 209× | 100% |

## Context compression — native vs compressed for bug fix

| Context | Tokens in | Tokens out | Time | Cost | GPT-4o equiv | Quality |
|---------|----------:|-----------:|-----:|-----:|-------------:|---------|
| native | 1,598 | 423 | 30.9s | $0.000276 | $0.008225 | correct fix |
| stub | 1,309 | 429 | 37.2s | $0.000255 | $0.007563 | correct fix |

**Compression saved 18% of input tokens and 8% of cost, same fix quality.**
