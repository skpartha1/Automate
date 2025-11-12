"""Import Azure resource groups and attempt to generate Terraform per resource group.

This script uses the Azure CLI to export ARM templates and attempts to run
`terraformer` to produce Terraform files. It validates outputs by running
`terraform init` and picks the provider output (if any) that succeeds.

Notes:
- Requires: `az`, `terraform`, and optionally `terraformer` on PATH.
- Authentication: interactive `az login` or Service Principal via env vars
  `AZCLIENTID`, `AZCLIENTSECRET`, `AZTENANTID` and pass `--use-sp`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple


INVALID_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def run(cmd: list[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    """Run command and return (returncode, stdout, stderr)."""
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def safe_name(name: str) -> str:
    return INVALID_NAME_RE.sub('-', name)


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def az_login(use_sp: bool) -> None:
    if use_sp:
        client_id = os.environ.get('AZCLIENTID')
        client_secret = os.environ.get('AZCLIENTSECRET')
        tenant_id = os.environ.get('AZTENANTID')
        if not (client_id and client_secret and tenant_id):
            raise SystemExit('Service principal requested but AZCLIENTID/AZCLIENTSECRET/AZTENANTID not set in environment')
        code, out, err = run(['az', 'login', '--service-principal', '-u', client_id, '-p', client_secret, '--tenant', tenant_id])
        if code != 0:
            raise SystemExit(f'az login (service-principal) failed: {err}')
    else:
        # Check existing session
        code, out, err = run(['az', 'account', 'show'])
        if code != 0:
            print('No active az session found — opening interactive login...')
            code2, out2, err2 = run(['az', 'login'])
            if code2 != 0:
                raise SystemExit(f'az login failed: {err2}')


def set_subscription(subscription_id: str) -> None:
    code, out, err = run(['az', 'account', 'set', '--subscription', subscription_id])
    if code != 0:
        raise SystemExit(f'Failed to set subscription {subscription_id}: {err}')


def list_resource_groups(subscription_id: str) -> list[dict]:
    code, out, err = run(['az', 'group', 'list', '--subscription', subscription_id, '-o', 'json'])
    if code != 0:
        raise SystemExit(f'Failed to list resource groups: {err}')
    return json.loads(out)


def export_arm_template(subscription_id: str, rg_name: str, out_path: Path) -> bool:
    # az group export outputs JSON to stdout; write to file
    code, out, err = run(['az', 'group', 'export', '-n', rg_name, '--subscription', subscription_id, '-o', 'json'])
    if code != 0:
        print(f'  az group export failed for {rg_name}: {err}')
        return False
    out_path.write_text(out)
    return True


def terraformer_import(subscription_id: str, rg_name: str, dest: Path, log_prefix: str) -> bool:
    # dest is the path terraformer will write to
    if dest.exists():
        shutil.rmtree(dest)
    ensure_dir(dest)
    stdout_log = dest.parent / f'{log_prefix}_out.log'
    stderr_log = dest.parent / f'{log_prefix}_err.log'
    cmd = ['terraformer', 'import', 'azure', f'--resource-group={rg_name}', f'--subscription={subscription_id}', f'--path={str(dest)}']
    print('   Running:', ' '.join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout_log.write_text(proc.stdout)
    stderr_log.write_text(proc.stderr)
    return proc.returncode == 0


def terraform_init_ok(path: Path, prefix: str) -> bool:
    # Run terraform init and capture output to files
    if not path.exists():
        return False
    stdout_file = path / f'init_{prefix}_out.txt'
    stderr_file = path / f'init_{prefix}_err.txt'
    proc = subprocess.run(['terraform', 'init', '-no-color'], cwd=str(path), capture_output=True, text=True)
    stdout_file.write_text(proc.stdout)
    stderr_file.write_text(proc.stderr)
    return proc.returncode == 0


def consolidate_tf(chosen_dir: Path, target_main: Path) -> None:
    # concatenate all .tf files under chosen_dir into target_main
    tf_files = sorted(chosen_dir.rglob('*.tf'))
    with target_main.open('w', encoding='utf-8') as out_f:
        for tf in tf_files:
            out_f.write(f'\n// ===== file: {tf.relative_to(chosen_dir)} =====\n')
            out_f.write(tf.read_text())


def process_resource_group(subscription_id: str, rg: dict, subs_folder: Path, has_terraformer: bool) -> None:
    rg_name = rg.get('name')
    print('Processing resource group:', rg_name)
    rg_folder = subs_folder / safe_name(rg_name)
    ensure_dir(rg_folder)

    arm_out = rg_folder / 'template.json'
    ok = export_arm_template(subscription_id, rg_name, arm_out)
    if not ok:
        print(f'  skipped {rg_name} due to export failure')
        return

    tf_azurerm = rg_folder / 'terraform_azurerm'
    tf_azapi = rg_folder / 'terraform_azapi'
    chosen = None

    if has_terraformer:
        print(' Attempting Terraformer import for AzureRM provider...')
        success = terraformer_import(subscription_id, rg_name, tf_azurerm, 'tf_azurerm')
        if success:
            print('  Terraformer AzureRM completed. Running terraform init...')
            if terraform_init_ok(tf_azurerm, 'azurerm'):
                chosen = tf_azurerm
                print('  AzureRM Terraform init succeeded.')
            else:
                print('  AzureRM terraform init failed. See logs.')
        else:
            print('  Terraformer AzureRM import failed. See logs.')

    if chosen is None and has_terraformer:
        print(' Attempting Terraformer import (second attempt - AZAPI style)...')
        success2 = terraformer_import(subscription_id, rg_name, tf_azapi, 'tf_azapi')
        if success2:
            if terraform_init_ok(tf_azapi, 'azapi'):
                chosen = tf_azapi
                print('  AZAPI-style Terraform init succeeded.')
            else:
                print('  AZAPI terraform init failed. See logs.')
        else:
            print('  Terraformer AZAPI attempt failed. See logs.')

    target_main = rg_folder / 'main.tf'
    if chosen is not None:
        print(' Consolidating .tf files into', target_main)
        consolidate_tf(chosen, target_main)
        print('  Wrote', target_main)
    else:
        print(' No working Terraform generated for', rg_name, '- ARM template saved.')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--subscription-id', required=True)
    parser.add_argument('-o', '--output-root', default='./output')
    parser.add_argument('--use-sp', action='store_true', help='Use service principal (AZCLIENTID/AZCLIENTSECRET/AZTENANTID env vars)')
    args = parser.parse_args()

    out_root = Path(args.output_root).resolve()
    ensure_dir(out_root)

    az_login(args.use_sp)
    set_subscription(args.subscription_id)

    subs_folder = out_root / args.subscription_id
    ensure_dir(subs_folder)

    # check terraformer presence
    has_terraformer = shutil.which('terraformer') is not None
    if not has_terraformer:
        print('Warning: terraformer not found on PATH. The script will export ARM templates but cannot auto-generate Terraform without terraformer.')

    rgs = list_resource_groups(args.subscription_id)
    if not rgs:
        print('No resource groups found in subscription', args.subscription_id)
        return

    for rg in rgs:
        process_resource_group(args.subscription_id, rg, subs_folder, has_terraformer)

    print('Done. Output written to', out_root / args.subscription_id)


if __name__ == '__main__':
    main()
