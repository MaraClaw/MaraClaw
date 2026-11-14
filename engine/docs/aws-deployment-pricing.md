# AWS deployment and pricing economics

Research date: November 14, 2026. Currency: USD, excluding tax. Region: Asia Pacific (Singapore), `ap-southeast-1`, unless stated otherwise. This note records why MaraClaw's commercial terms were re-priced around agents, and how its OpenClaw guests fit on AWS. The terms themselves, and what the engine enforces, live in [organization-pricing.md](organization-pricing.md). Guest limits, the workspace disk quota and resource-pressure records live in [guest-containers.md](guest-containers.md).

## Guests cannot run on Fargate as built

The engine drives every agent's OpenClaw guest through the Docker CLI on the host's Docker socket (`app/services/guest_*.py`, launched by `start-from-docker.sh`). Fargate offers none of what that needs:

| What the engine needs for each guest | Fargate |
| --- | --- |
| The Docker socket, for `docker run`, `update`, `stats`, `events` and `inspect --size` | Not available. Each agent would become an ECS task, which means rewriting guest creation and resume, tier limits, pressure sampling, the OOM event reader and the workspace disk quota. |
| bubblewrap capabilities (`SYS_ADMIN`, `SETUID`, `SETGID`, `SYS_CHROOT`, `SETPCAP`, `NET_ADMIN`, `SYS_PTRACE`) and `seccomp=unconfined` | Only `SYS_PTRACE` can be added, and privileged mode is not supported, so the sandbox cannot start. |
| The agent workspace as a host directory bind-mounted at `/home/node/custom-state` | No host directories. EFS would be needed. |
| Pro 1.5 vCPU and Ultra 3 vCPU | Not valid task sizes. Valid sizes include 1 vCPU (2 to 8 GB), 2 vCPU (4 to 16 GB) and 4 vCPU (8 to 30 GB). |
| Changing a stopped guest's limits in place (`docker update`) | Not possible. A new size needs a new task definition and a new task. |

**Recommended layout:** run the engine API, `web-e` and `web-a` on Fargate, and run the guests on Graviton EC2 Docker hosts (the `r8g` family; guest images are published for `linux/arm64`). The engine reads agent workspaces from `DATA_DIR` and bind-mounts the same path into each guest, so the engine roles that touch workspaces must share a filesystem with the guest host: either run on that host, or mount the same EFS path on both. More than one guest host also needs placement logic; aggregate admission today coordinates only cooperating starts on one host.

## Cost of one always-on agent

Fargate prices are AWS's published price file (`AmazonECS/current/ap-southeast-1`) on the research date: Linux/ARM costs $0.04045 per vCPU-hour and $0.00442 per GB-hour (x86: $0.05056 and $0.00553). EC2 prices are for an `r8g.2xlarge` (8 vCPU, 64 GiB): $0.568/hour on demand, $0.392/hour on a 1-year commitment. They assume 60 GiB given to guests, no memory overcommit, and 730 hours a month.

| Tier guest | Fargate ARM, nearest valid size | EC2 `r8g`, on demand | EC2 `r8g`, 1-year commitment |
| --- | ---: | ---: | ---: |
| Pro (2 GiB) | $36 (1 vCPU / 2 GB) or $72 (2 vCPU / 4 GB) | $13.8 (30 per host) | $9.5 |
| Max (3 GiB) | $72 (2 vCPU / 4 GB) | $20.7 (20 per host) | $14.3 |
| Ultra (5 GiB) | $144 (4 vCPU / 8 GB) | $34.6 (12 per host) | $23.9 |

Fargate costs 2.5 to 4 times more for an always-on agent, because it bills the full reserved size around the clock. It only pays off if agents sleep when idle: at 10 hours a day on 22 days a month, a Pro guest on Fargate costs about $11. Each wake would then pull the 1.96 GB guest image again, adding about 20 to 25 seconds (about 5 seconds with SOCI lazy loading) to the 10 to 15 seconds a guest takes to become ready. The engine has no idle auto-stop today: guests run until stopped, and agents send a heartbeat every 240 minutes by default.

Other per-use costs the platform carries:

- **Web search.** Tavily bills $0.008 per credit pay-as-you-go, falling to $0.005 on its Growth plan. A basic search is 1 credit and an advanced search is 2. An agent making 50 basic searches a day costs $7.5 to $12 a month, about as much as its compute.
- **Managed S3 storage.** About $0.025 per GB-month in Singapore (S3 Standard), so a full 10 GiB Ultra allowance costs about $0.25.
- **Payments.** Allow 5% of revenue. Stripe does not onboard merchants based in Vietnam (not rechecked on the research date), so a merchant of record such as Paddle (about 5% plus $0.50 per transaction) or a local payment gateway is likely.

## Why the November 13 terms lose money

Those terms charged per human account ($49 / $149 / $399 per organization; $6 / $5 / $4 per extra account) while costs come from agents (2 / 5 / 10 per account).

| Tier | Price | Agents allowed by the included accounts | Fargate cost at that allowance | Fargate break-even |
| --- | ---: | ---: | ---: | ---: |
| Pro | $49 | 10 | about $360 | 1.4 agents |
| Max | $149 | 75 | about $5,400 | 2.1 agents |
| Ultra | $399 | 400 | about $57,600 | 2.8 agents |

The additional account was the worst line: an extra Pro account cost $6 and brought 2 agents (about $28 on EC2, $72 on Fargate), and an extra Ultra account cost $4 and brought 10 agents (about $346 on EC2, $1,440 on Fargate).

## Adopted terms

Charge for agents, because they carry the cost, and keep human accounts cheap. Customers still pay for their own LLM inference. The recommendation was adopted on November 14, 2026 with three changes: the per-account agent cap stays (Pro 2, Max 3, Ultra 5) and a company's agents are capped at the included agents plus purchased extras, there is no account ceiling (companies buy extra accounts), there is no web-search allowance for now, and the add-ons are catalog fields. The authoritative terms and what the engine enforces are in [organization-pricing.md](organization-pricing.md).

| | Pro | Max | Ultra |
| --- | ---: | ---: | ---: |
| Organization fee per month / per year | $79 / $790 | $249 / $2,490 | $699 / $6,990 |
| Agents included | 2 | 5 | 10 |
| Each extra agent per month | $35 | $49 | $79 |
| Human accounts included / each extra per month | 5 / $5 | 15 / $4 | 40 / $3 |
| Agents per account (enforced) | 2 | 3 | 5 |
| Account ceiling | none; companies buy extra accounts | none; companies buy extra accounts | none; companies buy extra accounts |

Gross margin, every agent always on, guests on EC2 `r8g`:

| Scenario | Pro | Max | Ultra |
| --- | ---: | ---: | ---: |
| Included agents and accounts only, on demand | 42% | 45% | 41% |
| Included only, 1-year commitment | 53% | 58% | 57% |
| Example: 25 / 75 / 200 accounts, each at its agent cap (50 / 225 / 1,000 agents), on demand | 52% | 50% | 49% |
| Same, 1-year commitment | 63% | 62% | 62% |
| Each extra agent, on demand | 50% | 49% | 49% |

The margins count guest compute, $2 per agent for disk and backups, $10 per organization for its share of the shared platform (engine, database, Redis, load balancer, logs), and 5% payment fees. The per-agent and per-organization overheads are estimates, not measurements. Web search is not counted; without an allowance it is an open per-use cost (see above). The extra-agent price falls per GiB of guest memory as the tier rises ($17.5, $16.3 and $15.8), so larger tiers still get the better rate.

**These margins hold only if every agent beyond the included ones is billed.** The engine caps a company's ordinary agents at the tier's included agents plus purchased extra agents, and its accounts at the included accounts plus purchased extra accounts, so unbilled agents are bounded by what has been purchased rather than by accounts times the per-account cap. A company that holds extras it does not pay for would cost, at 50 Pro, 225 Max or 1,000 Ultra agents, about $800, $5,100 or $36,600 a month on on-demand EC2 against fees of $79, $249 and $699.

Further terms:

1. **Annual billing with two months free** (ten times the monthly fee). It funds a 1-year AWS commitment, which cuts compute cost by about 31% and pays for the discount. Make it the default for Ultra.
2. **Add-ons, the same for every tier:** +10 GiB managed storage for $5 a month (S3 costs about $0.25) and +5 GiB workspace disk for $3. Larger guests priced per GiB remain an idea. The platform-admin Upgrade signals screen already shows which agents run at 90% or more of a limit.
3. **Web search:** no allowance for now. If search cost grows, a per-agent monthly credit allowance (suggested Pro 300, Max 1,000, Ultra 3,000) with $10 packs of 1,000 credits, or an organization's own Tavily key, would bound it.
4. **Keep** the per-agent daily message caps, no service level in the base price, and customer-paid inference. Sell a service level, or Vietnam data residency, as Ultra or enterprise add-ons. AWS opened a Local Zone in Hanoi (`ap-southeast-1-han-1a`) in June 2026 with ECS, EKS, S3 One Zone-IA and x86 instances (C7i, M7i, R7i) only, so Graviton guests are not available there.

Later levers: hibernate idle guests on EC2, so a stopped guest frees its memory for others, and use resource-pressure records to pack more guests per host. On November 13, idle Pro guests used about 1.3 GiB of their 2 GiB.

## Sources

- AWS Fargate pricing and the price file `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/ap-southeast-1/index.json` (US East: ARM $0.03238 per vCPU-hour, $0.00356 per GB-hour).
- Amazon ECS task definition parameters for Fargate: valid CPU and memory combinations, `linuxParameters.capabilities.add` limited to `SYS_PTRACE`, no `privileged`, no `devices`.
- Compute Savings Plans: apply to Fargate vCPU and memory; up to 66% off, typically about 20% (1 year) to 45% (3 years) for Fargate with no upfront payment.
- `r8g.xlarge` and `r8g.2xlarge` price tables (instances.vantage.sh, devzero.io): Singapore on demand $0.284/hour per `r8g.xlarge`, 1-year reserved $0.196.
- AWS Seekable OCI (SOCI) on Fargate: about 5 seconds to start pulling a 1.3 to 2.5 GB image, against 20 to 25 seconds without an index.
- Tavily credits and pricing (`docs.tavily.com/documentation/api-credits`).
- AWS What's New, June 2026: Local Zone in Hanoi, Vietnam.
- Market references for agent and managed-OpenClaw pricing: Lindy ($29.99 per active user per month with 3,000 credits), Dust (Pro €30 per seat per month), Relevance AI (Team $349 per month), managed OpenClaw hosts ($4 to $79 per month per instance, usually without inference).

Guest memory, startup time and plugin figures were measured on local `arm64-openclaw:2026.11.3` guests on November 13 and 14, 2026, not on AWS.
