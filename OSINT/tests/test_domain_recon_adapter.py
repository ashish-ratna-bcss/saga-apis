import subprocess

import pytest

from osint_app.adapters import domain_recon_adapter
from osint_app.adapters.base import AdapterStatus, SourceUnavailable
from osint_app.enums import ClaimType


class _FakeCompletedProcess:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""


def _fake_run(cmd, **kwargs):
    if cmd[0] == "whois":
        return _FakeCompletedProcess("Domain Name: EXAMPLE.COM\n")
    record_type = cmd[1].split("=")[-1]
    target = cmd[2]
    if record_type == "ns":
        return _FakeCompletedProcess("example.com\tnameserver = ns1.example.com.\n")
    if record_type == "mx":
        return _FakeCompletedProcess("example.com\tmail exchanger = 10 mail.example.com.\n")
    if record_type == "txt" and target == "example.com":
        return _FakeCompletedProcess('example.com\ttext = "v=spf1 include:_spf.example.com ~all"\n')
    if record_type == "txt":
        return _FakeCompletedProcess('_dmarc.example.com\ttext = "v=DMARC1; p=none"\n')
    return _FakeCompletedProcess("")


@pytest.mark.asyncio
async def test_run_returns_technical_claim(monkeypatch):
    monkeypatch.setattr(domain_recon_adapter.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(domain_recon_adapter.subprocess, "run", _fake_run)

    adapter = domain_recon_adapter.DomainReconAdapter()
    assert await adapter.is_available() is True

    results = await adapter.run("example.com")
    assert len(results) == 1
    result = results[0]
    assert result.status == AdapterStatus.FOUND
    assert result.claim_type == ClaimType.TECHNICAL
    metadata = result.evidence.metadata
    assert metadata["ns_records"] == ["ns1.example.com"]
    assert metadata["mx_records"] == ["mail.example.com"]
    assert metadata["spf_present"] is True
    assert metadata["dmarc_present"] is True
    assert metadata["whois_available"] is True


@pytest.mark.asyncio
async def test_whois_binary_missing_still_returns_dns_records(monkeypatch):
    """whois is optional -- missing it must never block DNS/MX/SPF/DMARC,
    which only depend on nslookup."""

    def _which(tool):
        return "/usr/bin/nslookup" if tool == "nslookup" else None

    monkeypatch.setattr(domain_recon_adapter.shutil, "which", _which)
    monkeypatch.setattr(domain_recon_adapter.subprocess, "run", _fake_run)

    adapter = domain_recon_adapter.DomainReconAdapter()
    results = await adapter.run("example.com")

    assert len(results) == 1
    metadata = results[0].evidence.metadata
    assert metadata["ns_records"] == ["ns1.example.com"]
    assert metadata["mx_records"] == ["mail.example.com"]
    assert metadata["spf_present"] is True
    assert metadata["dmarc_present"] is True
    assert metadata["whois_available"] is False
    assert metadata["whois_preview"] is None


@pytest.mark.asyncio
async def test_whois_command_failure_still_returns_dns_records(monkeypatch):
    """whois binary present but the call itself fails (timeout) -- must
    degrade to whois_available=False without fabricating data, and must
    never fail the overall lookup since nslookup already succeeded."""

    def _run_whois_fails(cmd, **kwargs):
        if cmd[0] == "whois":
            raise subprocess.TimeoutExpired(cmd, 15)
        return _fake_run(cmd, **kwargs)

    monkeypatch.setattr(domain_recon_adapter.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(domain_recon_adapter.subprocess, "run", _run_whois_fails)

    adapter = domain_recon_adapter.DomainReconAdapter()
    results = await adapter.run("example.com")

    assert len(results) == 1
    metadata = results[0].evidence.metadata
    assert metadata["ns_records"] == ["ns1.example.com"]
    assert metadata["spf_present"] is True
    assert metadata["whois_available"] is False
    assert metadata["whois_preview"] is None


@pytest.mark.asyncio
async def test_unavailable_without_nslookup(monkeypatch):
    monkeypatch.setattr(domain_recon_adapter.shutil, "which", lambda tool: None)

    adapter = domain_recon_adapter.DomainReconAdapter()
    assert await adapter.is_available() is False
    with pytest.raises(SourceUnavailable):
        await adapter.run("example.com")


@pytest.mark.asyncio
async def test_subprocess_failure_raises_unavailable(monkeypatch):
    def _raise(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(domain_recon_adapter.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(domain_recon_adapter.subprocess, "run", _raise)

    adapter = domain_recon_adapter.DomainReconAdapter()
    with pytest.raises(SourceUnavailable):
        await adapter.run("example.com")
