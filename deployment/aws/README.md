# AWS: temporary EC2 workers for large cloud-to-cloud copies

The central Motuz node (the EC2 instance running the web app, Celery and the token broker) can
start a short-lived EC2 worker for a large cloud-to-cloud copy. The worker runs a `motuz-worker`
agent that talks to the central node only over HTTPS (443) with a single-use bootstrap token from
its user-data, runs one job, and shuts down; shutdown terminates the instance.

Workers get **no data permissions through IAM**. They use the S3 (or other) credentials of the
user's Motuz connection, delivered in a scoped job ticket. The IAM here only controls who may
start and stop workers and what those workers look like.

This directory holds the IAM policies and launch template data. `${VARS}` are filled in with
`envsubst` (see "Recreate in another account").

| File | What |
|---|---|
| `central-launch-workers-policy.json` | Inline policy `motuz-launch-workers` on the central node's role |
| `worker-trust-policy.json` | Trust policy of the `motuz-worker` role (ec2.amazonaws.com only) |
| `worker-boundary-policy.json` | Permissions boundary `motuz-worker-boundary`: at most Session Manager |
| `worker-ssm-debug-policy.json` | Optional `motuz-worker-ssm-debug`, **not attached**: Session Manager shell on workers |
| `launch-template-amd64.json`, `launch-template-arm64.json` | Launch template data, Amazon Linux 2027 preview (default version) |
| `launch-template-ubuntu-amd64.json`, `launch-template-ubuntu-arm64.json` | Launch template data, Ubuntu 26.04 (fallback version 1) |

## Design

### Worker network: security group `motuz-worker`

- **No inbound rules.** Nothing connects to a worker; the agent polls the central node.
- **Outbound TCP 443 only**, to `0.0.0.0/0` and `::/0` (the default VPC has no IPv6 today, so the
  `::/0` rule is inert until it does). 443 to anywhere is needed because the storage APIs (S3,
  Azure Blob, GCS, Google Drive, OneDrive) and the central node (its public Elastic IP) are all
  reached on 443.
- **No DNS or NTP rules.** Security groups do not filter traffic to the Amazon DNS resolver (VPC
  base + 2 and 169.254.169.253), the Amazon Time Sync Service (169.254.169.123), instance metadata
  (169.254.169.254) or DHCP. Amazon Linux points chrony at 169.254.169.123 by default. Ubuntu's
  default time sources (ntp.ubuntu.com, NTS on TCP 4460) are blocked, so Ubuntu user-data should
  add `server 169.254.169.123 prefer iburst` to chrony.
- Consequences of 443-only: plain-HTTP package mirrors (Ubuntu's `*.ec2.archive.ubuntu.com` on port
  80) do not work; fetch rclone and the agent over HTTPS (ideally from the central node, checksum
  verified). SFTP (22), FTP, and WebDAV/S3-compatible endpoints on other ports are unreachable, so
  only offload jobs whose both ends are HTTPS APIs.
- Workers get a public IPv4 address from the default subnet (`MapPublicIpOnLaunch`, billed at
  0.005 USD/h while running). The SG keeps them unreachable from outside.

### Worker role `motuz-worker`

- Trust: `ec2.amazonaws.com` only. Permissions: **none**. Instance profile of the same name.
- Permissions boundary `motuz-worker-boundary` allows at most the five Session Manager actions
  (`ssm:UpdateInstanceInformation`, `ssmmessages:{Create,Open}{Control,Data}Channel`). Whatever is
  attached later, the role can never get data access (S3, KMS, ...).
- For debugging, attach `motuz-worker-ssm-debug` (the same five actions) to the role; running
  workers pick it up within minutes, then `aws ssm start-session --target <worker-id>`. Detach it
  again afterwards. Not attached by default: a shell on a worker exposes the job ticket (user
  credentials).
- An empty role (instead of no instance profile) keeps the SSM debug option and the boundary in
  place. Its IMDS credentials can only call `sts:GetCallerIdentity`.

### Launch templates `motuz-worker` (x86_64) and `motuz-worker-arm64` (Graviton)

One template per architecture, because an AMI is architecture specific and the central policy
ties each template to instance types of its architecture.

| Version | Image (resolved at launch) | Root device |
|---|---|---|
| 1 | Ubuntu 26.04: `resolve:ssm:/aws/service/canonical/ubuntu/server/26.04/stable/current/{amd64,arm64}/hvm/ebs-gp3/ami-id` | `/dev/sda1` |
| 2 (**default**) | Amazon Linux 2027 **preview**: `resolve:ssm:/aws/service/ami-amazon-linux-latest/al2027-preview-ami-kernel-default-{x86_64,arm64}` | `/dev/xvda` |

Both versions: instance profile `motuz-worker`, SG `motuz-worker`, no key pair, IMDSv2 required
with hop limit 1 (instance metadata tags off), `InstanceInitiatedShutdownBehavior=terminate`,
`DisableApiStop=true`, 16 GiB gp3 root, encrypted (AWS managed key `aws/ebs`) and deleted on
termination, detailed monitoring off, tags `Project=motuz`, `Component=worker`,
`ManagedBy=motuz-central` on instance and volume. No subnet (the launcher picks one) and no
user-data (it comes per launch).

AL2027 is a public preview (since 2026-09-03): not for production, AMIs carry a deprecation date,
and the parameter names may change at GA. SELinux is **enforcing** by default, which matters for the
worker image: install binaries under standard paths (`/usr/local/bin`), run `restorecon`, and use
plain systemd units. AL2023's package repositories are served over HTTPS, which fits the 443-only
SG; expect the same for AL2027 but check it on the first real launch. To go back to Ubuntu, launch with `Version=1` or make version 1 the default:

    aws --profile $PROFILE --region $REGION ec2 modify-launch-template --launch-template-name motuz-worker --default-version 1

If the AL2027 SSM parameter goes away, look up the AMI by name and put the id into a new template
version (the central policy accepts any image that comes from the template and is owned by Amazon
Linux or Canonical):

    aws --profile $PROFILE --region $REGION ec2 describe-images --owners amazon \
      --filters 'Name=name,Values=al2027-preview-ami-2027.*-kernel-*-x86_64' \
      --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name]' --output text   # arm64: ...-arm64

### Central node policy `motuz-launch-workers`

Added as an **inline policy on the existing central role** (`motuz-ssm` in the osu account)
rather than creating a new role and swapping the instance profile. Swapping the profile
(`replace-iam-instance-profile-association`) briefly changes the instance's credentials and the SSM
agent has to pick up the new role; since SSM is the only way in, a mistake there locks us out.
Adding an inline policy is a single, instantly reversible call (`delete-role-policy`) that leaves
SSM untouched, and the policy is deleted together with the role. The cost is a role name that no
longer describes everything the role does.

Statements (all resources scoped to one region and account):

| Sid | Allows |
|---|---|
| `RunInstancesUseWorkerLaunchTemplates` | the two launch-template ARNs |
| `RunInstancesWorkerInstanceAmd64` / `...Arm64` | `instance/*` only with that template (`ec2:LaunchTemplate`), an instance type from its list, request tags `Project=motuz`, `Component=worker`, tag keys only from `Project Component ManagedBy Name MotuzJob`, instance profile `motuz-worker`, IMDSv2 required, hop limit <= 1, on-demand, default tenancy |
| `RunInstancesWorkerRootVolume` | `volume/*` from a worker template, encrypted, gp3, <= 64 GiB |
| `RunInstancesDefaultVpcOnly` | `subnet/*`, `network-interface/*` from a worker template, in the default VPC (`ec2:Vpc`) |
| `RunInstancesWorkerSecurityGroupOnly` | only `security-group/<motuz-worker>` |
| `RunInstancesTemplateImageOnly` | `image/*` only from the template (`ec2:IsLaunchTemplateResource=true`) and owned by Amazon Linux (137112412989) or Canonical (099720109477) (`aws:ResourceAccount`; note `ec2:Owner` is `amazon` for both) |
| `TagWorkersOnlyWhileLaunching` | `ec2:CreateTags` on instances/volumes only with `ec2:CreateAction=RunInstances` and the worker tags |
| `PassOnlyTheWorkerRoleToEc2` | `iam:PassRole` for `role/motuz-worker`, `iam:PassedToService=ec2.amazonaws.com` |
| `TerminateOnlyMotuzWorkers` | `ec2:TerminateInstances` on instances tagged `Project=motuz` **and** `Component=worker` (the central node has no `Component` tag and cannot add one) |
| `DescribeInstancesNotResourceScopable` | `ec2:DescribeInstances`, `ec2:DescribeInstanceStatus` on `*` in this region: these actions do not support resource-level permissions, so they reveal all instances in the region (metadata only, not user-data) |
| `ResolveWorkerAmiParameters` | `ssm:GetParameters` on the four public AMI parameters. A dry run succeeded without it (EC2 resolves `resolve:ssm` itself), so it is insurance for real launches and lets the launcher log the AMI id; the data is public |

No `ec2:*` or `iam:*` wildcards; no Stop, ModifyInstanceAttribute, CreateLaunchTemplateVersion,
DescribeInstanceAttribute (user-data) or any data-plane permission.

Allowed instance types (network optimized; the upper bound caps one worker at about 1 USD/h):

| Template | Types |
|---|---|
| `motuz-worker` (x86_64) | `c6in.{large,xlarge,2xlarge,4xlarge}` (up to 25/30/40/50 Gbps), `c7i.{large,xlarge,2xlarge}` (cheaper, up to 12.5 Gbps) |
| `motuz-worker-arm64` | `c7gn.{large,xlarge,2xlarge,4xlarge}` (up to 30/40/50/50 Gbps), `c8gn.{large,xlarge,2xlarge,4xlarge}` (Graviton4) |

Larger sizes (c7gn.16xlarge and c6in.32xlarge reach 200 Gbps) are one list edit away. A single TCP
flow to the internet is limited to 5 Gbps, so high throughput needs many parallel transfers.

### What IAM cannot enforce

- **Instance count.** RunInstances has no condition key for the count, and AWS has no per-tag
  instance limit. The launcher must enforce a maximum number of running workers (count
  `DescribeInstances` with `tag:Project=motuz`, `tag:Component=worker`, state pending/running) and a
  maximum lifetime, and a reaper on the central node must terminate workers that outlive their job
  or are `stopped`. The only account-wide cap is the EC2 On-Demand vCPU quota
  (L-1216C47A, 512 vCPUs here, shared with other workloads); Service Quotas cannot lower it.
- **Shutdown behavior.** There is no condition key for `InstanceInitiatedShutdownBehavior` or
  `DisableApiStop`; a request could override them. A worker that stops instead of terminating
  only costs its 16 GiB volume and stays terminable by the central role; the reaper should
  terminate stopped workers. The launcher must not pass these parameters.
- **Tags in the request.** Launch template tags count as request tags (`aws:RequestTag`), so a
  RunInstances call without its own tags is allowed (it still gets the template's tags). A request
  that sets a different value (`Component=web`) or an unlisted key is denied.

## What the launcher must pass

    ec2.run_instances(
        LaunchTemplate={'LaunchTemplateName': 'motuz-worker',        # or 'motuz-worker-arm64'
                        'Version': '$Default'},                       # '1' = Ubuntu fallback
        InstanceType='c6in.xlarge',              # from that template's allowed list
        MinCount=1, MaxCount=1,
        SubnetId='subnet-...',                   # optional; a default subnet (choose the AZ)
        UserData=script,                         # <= 16 KB before base64 (boto3 encodes it)
        ClientToken=f'copy-{job_id}-{attempt}',  # idempotent retries
        TagSpecifications=[{'ResourceType': t, 'Tags': [
            {'Key': 'Project', 'Value': 'motuz'}, {'Key': 'Component', 'Value': 'worker'},
            {'Key': 'ManagedBy', 'Value': 'motuz-central'},
            {'Key': 'Name', 'Value': f'motuz-worker-copy-{job_id}'},
            {'Key': 'MotuzJob', 'Value': f'copy-{job_id}'}]} for t in ('instance', 'volume')])

Do **not** pass `ImageId`, `SecurityGroupIds`, `NetworkInterfaces`, `IamInstanceProfile`, `KeyName`,
`BlockDeviceMappings`, `MetadataOptions`, `Placement.Tenancy`, `InstanceMarketOptions`,
`InstanceInitiatedShutdownBehavior` or `DisableApiStop`; most are denied, the rest would weaken the
template. Only the tag keys `Project Component ManagedBy Name MotuzJob` are allowed.

User-data is readable by every process on the worker (IMDS) and by account principals with
`ec2:DescribeInstanceAttribute`, so the bootstrap token must be single use and short lived. The
central node uses its instance role through IMDSv2 (hop limit 1 works because the Motuz containers
use host networking).

## Cost guardrails

- IAM, the instance profile, the security group and launch templates are free. Workers cost only
  while they run: the instance, 16 GiB gp3, the public IPv4 address and data transfer.
- A **budget alert** needs a subscriber (email or SNS topic), so it is a manual step. AWS Budgets
  charges only for action-enabled budgets beyond the first two; plain alert budgets are free.
- Filtering a budget by `Project=motuz` needs `Project` activated as a **cost allocation tag**. In
  an AWS Organizations member account this can only be done in the management (payer) account, and
  it changes the organization's billing reports. Without it, filter the budget by service
  (EC2-Instances) and linked account instead.
- Optional: a Budgets action "apply IAM policy" can attach a deny-`ec2:RunInstances` policy to the
  central role when a threshold is crossed (needs a Budgets execution role; the first two
  action-enabled budgets are free).

## Recreate in another account

Needs the AWS CLI v2 and `envsubst` (gettext). Run from this directory.

    PROFILE=<profile>; REGION=<region>
    A="aws --profile $PROFILE --region $REGION"
    export REGION
    export ACCOUNT_ID=$($A sts get-caller-identity --query Account --output text)
    export VPC_ID=$($A ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
    CENTRAL_ROLE=motuz-ssm        # the role in the central node's instance profile (see below)
    TAGS='{Key=Project,Value=motuz},{Key=Component,Value=workers}'
    W=$(mktemp -d)

    # 1. Security group: no inbound, only 443 out
    export WORKER_SG_ID=$($A ec2 create-security-group --group-name motuz-worker --vpc-id $VPC_ID \
      --description "Motuz temporary copy workers: no inbound, HTTPS out only" \
      --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=motuz-worker},$TAGS]" \
      --query GroupId --output text)
    $A ec2 revoke-security-group-egress --group-id $WORKER_SG_ID \
      --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
    $A ec2 authorize-security-group-egress --group-id $WORKER_SG_ID --ip-permissions \
      '[{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrIp":"0.0.0.0/0"}],"Ipv6Ranges":[{"CidrIpv6":"::/0"}]}]'

    # 2. Worker role with boundary, instance profile, optional debug policy (not attached)
    IT="Key=Project,Value=motuz Key=Component,Value=workers"
    $A iam create-policy --policy-name motuz-worker-boundary --policy-document file://worker-boundary-policy.json --tags $IT
    $A iam create-policy --policy-name motuz-worker-ssm-debug --policy-document file://worker-ssm-debug-policy.json --tags $IT
    $A iam create-role --role-name motuz-worker --assume-role-policy-document file://worker-trust-policy.json \
      --permissions-boundary arn:aws:iam::$ACCOUNT_ID:policy/motuz-worker-boundary --tags $IT
    $A iam create-instance-profile --instance-profile-name motuz-worker --tags $IT
    $A iam add-role-to-instance-profile --instance-profile-name motuz-worker --role-name motuz-worker
    sleep 10   # instance profile propagation

    # 3. Launch templates: version 1 Ubuntu 26.04, version 2 AL2027 (default)
    for arch in amd64 arm64; do
      name=motuz-worker; [ $arch = arm64 ] && name=motuz-worker-arm64
      envsubst '${WORKER_SG_ID}' < launch-template-ubuntu-$arch.json > $W/u.json
      envsubst '${WORKER_SG_ID}' < launch-template-$arch.json > $W/al.json
      id=$($A ec2 create-launch-template --launch-template-name $name --version-description "Ubuntu 26.04 $arch worker" \
        --launch-template-data file://$W/u.json --tag-specifications "ResourceType=launch-template,Tags=[$TAGS]" \
        --query LaunchTemplate.LaunchTemplateId --output text)
      $A ec2 create-launch-template-version --launch-template-id $id --launch-template-data file://$W/al.json \
        --version-description "Amazon Linux 2027 preview $arch worker (default)"
      $A ec2 modify-launch-template --launch-template-id $id --default-version 2
      [ $arch = amd64 ] && export LT_AMD64_ID=$id || export LT_ARM64_ID=$id
    done

    # 4. Central node policy
    envsubst '${ACCOUNT_ID} ${REGION} ${VPC_ID} ${WORKER_SG_ID} ${LT_AMD64_ID} ${LT_ARM64_ID}' \
      < central-launch-workers-policy.json > $W/central.json
    $A iam put-role-policy --role-name $CENTRAL_ROLE --policy-name motuz-launch-workers --policy-document file://$W/central.json

If the central node has no instance profile yet, create a role (trust `ec2.amazonaws.com`) with
`AmazonSSMManagedInstanceCore` plus this inline policy and associate it with
`aws ec2 associate-iam-instance-profile`. The launch-template ids are in the policy: recreating a
template means re-running step 4.

### Verify

Policy simulator (from an admin workstation), e.g. the allowed launch:

    $A iam simulate-principal-policy --policy-source-arn arn:aws:iam::$ACCOUNT_ID:role/$CENTRAL_ROLE \
      --action-names ec2:RunInstances --resource-arns "arn:aws:ec2:$REGION:$ACCOUNT_ID:instance/*" \
      --context-entries \
        ContextKeyName=ec2:LaunchTemplate,ContextKeyType=string,ContextKeyValues=arn:aws:ec2:$REGION:$ACCOUNT_ID:launch-template/$LT_AMD64_ID \
        ContextKeyName=ec2:InstanceType,ContextKeyType=string,ContextKeyValues=c6in.large \
        ContextKeyName=aws:RequestTag/Project,ContextKeyType=string,ContextKeyValues=motuz \
        ContextKeyName=aws:RequestTag/Component,ContextKeyType=string,ContextKeyValues=worker \
        ContextKeyName=aws:TagKeys,ContextKeyType=stringList,ContextKeyValues=Project,Component \
        ContextKeyName=ec2:InstanceProfile,ContextKeyType=string,ContextKeyValues=arn:aws:iam::$ACCOUNT_ID:instance-profile/motuz-worker \
        ContextKeyName=ec2:MetadataHttpTokens,ContextKeyType=string,ContextKeyValues=required \
        ContextKeyName=ec2:MetadataHttpEndpoint,ContextKeyType=string,ContextKeyValues=enabled \
        ContextKeyName=ec2:InstanceMetadataTags,ContextKeyType=string,ContextKeyValues=disabled \
        ContextKeyName=ec2:MetadataHttpPutResponseHopLimit,ContextKeyType=numeric,ContextKeyValues=1 \
        ContextKeyName=ec2:InstanceMarketType,ContextKeyType=string,ContextKeyValues=on-demand \
        ContextKeyName=ec2:Tenancy,ContextKeyType=string,ContextKeyValues=default

The simulator evaluates one context for all resources and does not know which keys EC2 fills in
from the template, so pass the keys explicitly. The real test is a dry run **on the central node**
with its instance role (`sudo snap install aws-cli --classic` if `aws` is missing):

    aws --region $REGION ec2 run-instances --dry-run --launch-template LaunchTemplateName=motuz-worker \
      --instance-type c6in.large    # -> DryRunOperation (allowed)
    aws --region $REGION ec2 run-instances --dry-run --image-id <ami> --instance-type c6in.large \
      --security-group-ids $WORKER_SG_ID   # -> UnauthorizedOperation

Decode a denial with `aws sts decode-authorization-message` (admin) to see the evaluated context.

## Teardown

    $A iam delete-role-policy --role-name $CENTRAL_ROLE --policy-name motuz-launch-workers
    $A ec2 delete-launch-template --launch-template-name motuz-worker
    $A ec2 delete-launch-template --launch-template-name motuz-worker-arm64
    $A iam remove-role-from-instance-profile --instance-profile-name motuz-worker --role-name motuz-worker
    $A iam delete-instance-profile --instance-profile-name motuz-worker
    $A iam detach-role-policy --role-name motuz-worker --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/motuz-worker-ssm-debug  # if attached
    $A iam delete-role --role-name motuz-worker
    $A iam delete-policy --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/motuz-worker-ssm-debug
    $A iam delete-policy --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/motuz-worker-boundary
    $A ec2 delete-security-group --group-id $WORKER_SG_ID   # after all workers are gone
