# Recovery drill archive

This directory records the outcomes and repeatable lessons from resilience and security drills.
The public summaries intentionally omit live resource identifiers, credentials, customer-derived
counts, network ranges, exact execution times, and detailed incident diagnostics. Store raw logs,
screenshots, traces, and other drill evidence in an access-controlled system with an appropriate
retention policy.

Each drill should establish:

1. the control and failure mode being tested;
2. a safe rollback and an observer who can stop the exercise;
3. success criteria before the exercise starts;
4. validation through the same path used by real traffic, without using customer data; and
5. cleanup, drift detection, and follow-up ownership.

Repeat a drill after material changes to the control it covers. Record only the minimum public
detail needed to explain what was tested, whether it passed, and what future maintainers should
verify.
