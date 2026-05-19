# Juju model checkpoint OpenStack PoC

This is a proof of concept for the 0.9 OpenStack-backed lab environment. It is
not a Juju source change and it does not modify zaza tests.

The goal is to validate whether an already deployed Juju model can be returned
to a clean workload state faster than redeploying the full zaza bundle.

## Why this lives in stsstack-bundles first

A production-quality Juju model snapshot feature would belong in Juju and would
need provider-level support, controller database coordination, and agent
quiescing.

This PoC is intentionally narrower:

* OpenStack backend only.
* Existing model only; no model clone.
* Existing Nova servers only; restore uses in-place rebuild.
* Refuse restore if the Juju model UUID or machine instance IDs changed.
* zaza tests and charm bundles are not changed.

That makes `stsstack-bundles` a better first location because the behavior is
specific to the self-hosted OpenStack test environment and can be tested without
changing Juju itself.

## Basic flow

Check whether a model can be checkpointed:

```bash
openstack/tools/juju_model_checkpoint_openstack.py check \
  -m juju-os-controller:admin/my-model
```

Create a checkpoint:

```bash
openstack/tools/juju_model_checkpoint_openstack.py create \
  -m juju-os-controller:admin/my-model \
  --name designate-focal-xena-clean
```

Run any manual test that does not change model topology.

Dry-run restore:

```bash
openstack/tools/juju_model_checkpoint_openstack.py restore \
  -m juju-os-controller:admin/my-model \
  ~/.local/share/juju-model-checkpoints/designate-focal-xena-clean-YYYYMMDDTHHMMSSZ
```

Restore for real:

```bash
openstack/tools/juju_model_checkpoint_openstack.py restore \
  -m juju-os-controller:admin/my-model \
  ~/.local/share/juju-model-checkpoints/designate-focal-xena-clean-YYYYMMDDTHHMMSSZ \
  --yes
```

Delete checkpoint images and metadata:

```bash
openstack/tools/juju_model_checkpoint_openstack.py delete \
  ~/.local/share/juju-model-checkpoints/designate-focal-xena-clean-YYYYMMDDTHHMMSSZ \
  --yes
```

## Current limitations

The checkpoint captures Juju status, show-model output, an exported bundle, and
Nova server images. It does not restore Juju controller database state, relation
data, secrets, actions, or operation history.

Use this first with a simple test model or with tests that mutate only workload
disk/data state. If a test changes Juju topology, charm revisions, relations, or
model-level state after the checkpoint, restore is intentionally not guaranteed
to produce a clean model.

## zaza runner flow

`charmed_openstack_functest_runner_juju_os.sh` has a checkpoint mode for the
same PoC. It keeps the zaza tests unchanged but splits the zaza phases so a
checkpoint can be taken after `deploy` and `configure`.

Create a test-ready checkpoint from a full bundle deployment:

```bash
~/stsstack-bundles/openstack/tools/charmed_openstack_functest_runner_juju_os.sh \
  --func-test-target focal-xena \
  --skip-build \
  --checkpoint-create designate-yoga-focal-xena \
  --checkpoint-verify-images
```

That command leaves the Juju model in place because this PoC restores in place.
`--checkpoint-verify-images` is slower because it downloads every snapshot
image once, but it catches Glance/Nova image corruption before the checkpoint is
treated as reusable.

Later, restore that model to the checkpoint state and run `functest-test`.
Pass the same `--func-test-target` so the runner reports the result against the
right zaza job:

```bash
~/stsstack-bundles/openstack/tools/charmed_openstack_functest_runner_juju_os.sh \
  --func-test-target focal-xena \
  --skip-build \
  --checkpoint-restore ~/juju-model-checkpoints/designate-yoga-focal-xena-YYYYMMDDTHHMMSSZ \
  --checkpoint-reset-after-test
```

With `--checkpoint-reset-after-test`, the runner restores the checkpoint again
after the zaza test phase, leaving the model ready for another test run.

This mode intentionally does not destroy and recreate the model. Destroying the
model removes the Juju controller state and Nova instance IDs that the current
PoC uses to validate restore safety.
