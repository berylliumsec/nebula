# Security policy

## Report a vulnerability privately

Use a private channel for suspected vulnerabilities in Nebula. Do not disclose
the details in public issues, Discussions, pull requests, logs, or screenshots.
For ordinary bugs and usage questions, see [Support](SUPPORT.md).

Email vulnerability reports privately to
[info@berylliumsec.com](mailto:info@berylliumsec.com).

Include only the information needed to assess the concern:

- The affected Nebula version or source commit and installation method
- The affected component, platform, and relevant configuration, without secrets
- A description of the security impact and the boundary you believe is affected
- Safe observations from an environment you own or are authorized to test
- Any known workaround, and how you would like to be credited, if applicable

Do not send credentials, tokens, private keys, customer data, real engagement
evidence, or unrelated personal information. Keep detailed evidence private
while coordinating with maintainers.

## Versions and scope

Nebula 3 is currently a preview. Check
[published releases](https://github.com/berylliumsec/nebula/releases) and the
[README](README.md#install-the-preview) for available builds and installation
guidance. Source checkouts may contain changes not present in a published build.
Include the exact affected version even when the issue concerns an older release.
This policy does not establish a maintenance window or promise backports.

This reporting process covers vulnerabilities in Nebula and its distributed
components. Findings in a third-party system or dependency should also go to its
owner through that project's reporting process; do not publish a third party's
private information here.

## Safe investigation and disclosure

Only test systems and data you own or have explicit authorization to test. Use
isolated environments and synthetic data, and stop if testing could expose
someone else's information or disrupt their service. Do not expand testing to
third-party systems to demonstrate impact.

Coordinate public disclosure with maintainers so affected users can receive
accurate mitigation information. This policy does not promise a response time,
fix deadline, reward, or support agreement.
