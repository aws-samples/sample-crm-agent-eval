#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Read-only pre-apply checks on the adopted VPC.
#
# Invoke before `scripts/deploy.sh`, with credentials for the target account:
#   scripts/preflight_adopted_vpc.sh --profile default --region us-east-1 \
#       --vpc-id vpc-xxxx --private-subnet-ids subnet-a,subnet-b \
#       --route-table-ids rtb-a,rtb-b [--nat-subnet-id subnet-c]
#
# Every call is a `describe-*`. It creates nothing, changes nothing, and exits non-zero on any refusal.
# Requires: aws CLI, jq, and credentials for the target account.

set -euo pipefail

PROFILE=""
REGION=""
VPC_ID=""
PRIVATE_SUBNET_IDS=""
ROUTE_TABLE_IDS=""
NAT_SUBNET_ID=""
# Optional AgentCore Runtime VPC-mode subnet(s) and route table(s).
RUNTIME_SUBNET_IDS=""
RUNTIME_ROUTE_TABLE_IDS=""
MIN_FREE_IPS=8
# 1 when --min-free-ips was passed, so the run can report which floor it used.
MIN_FREE_IPS_EXPLICIT=0
# `service` tag value marking a gateway endpoint this stack manages; untagged endpoints are foreign.
MANAGED_SERVICE_TAG="crm-agent-eval"

die() { printf 'preflight_adopted_vpc: REFUSED — %s\n' "$1" >&2; exit 1; }
info() { printf 'preflight_adopted_vpc: %s\n' "$1"; }

usage() {
    cat <<'USAGE'
Read-only pre-apply checks on the adopted VPC.

Required:
  --vpc-id <id>                    the VPC being adopted
  --private-subnet-ids <a,b>       comma-separated; the subnets Lambda ENIs go in
  --route-table-ids <a,b>          comma-separated; the tables receiving the NAT route
Optional:
  --nat-subnet-id <id>             the IGW-routed subnet the NAT gateway lands in
  --runtime-subnet-ids <a,b>       comma-separated; the AgentCore Runtime's VPC-mode subnets.
                                   Each must be in an AgentCore-supported AZ (read from
                                   infra/regions.json), off the main table, and have no
                                   internet-gateway route.
  --runtime-route-table-ids <a,b>  comma-separated; the Runtime subnets' own route tables.
  --min-free-ips <n>               free-address floor per adopted subnet (default 8 — the ALB's own
                                   scaling budget, NOT a subnet total; must rise above 8 once an ALB
                                   shares the subnet. See the note beside the check.)
  --profile <name>                 AWS profile (else AWS_PROFILE, else the default chain)
  --region <name>                  AWS region (else AWS_REGION)
  -h | --help

Exits 0 only if every check passes. Any refusal is non-zero and names the resource.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="${2:-}"; shift 2 ;;
        --region) REGION="${2:-}"; shift 2 ;;
        --vpc-id) VPC_ID="${2:-}"; shift 2 ;;
        --private-subnet-ids) PRIVATE_SUBNET_IDS="${2:-}"; shift 2 ;;
        --route-table-ids) ROUTE_TABLE_IDS="${2:-}"; shift 2 ;;
        --nat-subnet-id) NAT_SUBNET_ID="${2:-}"; shift 2 ;;
        --runtime-subnet-ids) RUNTIME_SUBNET_IDS="${2:-}"; shift 2 ;;
        --runtime-route-table-ids) RUNTIME_ROUTE_TABLE_IDS="${2:-}"; shift 2 ;;
        --min-free-ips) MIN_FREE_IPS="${2:-}"; MIN_FREE_IPS_EXPLICIT=1; shift 2 ;;
        --managed-service-tag) MANAGED_SERVICE_TAG="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

# Resolution order matches scripts/deploy.sh and src/crm/config.py: flag -> environment -> unset.
RESOLVED_PROFILE="${PROFILE:-${AWS_PROFILE:-}}"
RESOLVED_REGION="${REGION:-${AWS_REGION:-}}"
[[ -n "$RESOLVED_REGION" ]] || die "no region: pass --region or export AWS_REGION. Refusing rather than inheriting an ambient region."
[[ -n "$VPC_ID" ]] || die "no --vpc-id"
[[ -n "$PRIVATE_SUBNET_IDS" ]] || die "no --private-subnet-ids"
[[ -n "$ROUTE_TABLE_IDS" ]] || die "no --route-table-ids"

command -v aws >/dev/null 2>&1 || die "aws CLI not on PATH"
command -v jq >/dev/null 2>&1 || die "jq not on PATH"

# --region on every call, so no ambient region is used.
AWS_ARGS=(--region "$RESOLVED_REGION" --no-cli-pager --output json)
[[ -n "$RESOLVED_PROFILE" ]] && AWS_ARGS+=(--profile "$RESOLVED_PROFILE")

IFS=',' read -r -a SUBNETS <<< "$PRIVATE_SUBNET_IDS"
IFS=',' read -r -a TABLES <<< "$ROUTE_TABLE_IDS"

info "region=${RESOLVED_REGION} profile=${RESOLVED_PROFILE:-<default chain>} vpc=${VPC_ID}"
info "checking ${#SUBNETS[@]} private subnet(s) and ${#TABLES[@]} route table(s)"
# An empty list would let every loop below pass without examining anything.
(( ${#SUBNETS[@]} > 0 )) || die "--private-subnet-ids parsed to zero ids, so nothing would be checked"
(( ${#TABLES[@]} > 0 )) || die "--route-table-ids parsed to zero ids, so nothing would be checked"

# ---------------------------------------------------------------------------
# 0. The subnets and tables actually belong to the named VPC.
# ---------------------------------------------------------------------------
for subnet in "${SUBNETS[@]}"; do
    owner=$(aws ec2 describe-subnets "${AWS_ARGS[@]}" --subnet-ids "$subnet" \
        --query 'Subnets[0].VpcId' --output text) \
        || die "cannot describe subnet ${subnet} (does it exist in ${RESOLVED_REGION}?)"
    [[ "$owner" == "$VPC_ID" ]] \
        || die "subnet ${subnet} belongs to ${owner}, not the adopted VPC ${VPC_ID}"
done

# ---------------------------------------------------------------------------
# 0b. Each adopted subnet has room for the ENIs this stack puts in it.
# ---------------------------------------------------------------------------
# Each subnet's free addresses are shared by one interface-endpoint ENI per service, the Lambda
# Hyperplane ENIs, and an internal ALB (AWS asks for at least eight free addresses per ALB subnet).
# The default floor of 8 covers the ALB alone; derive a real floor from `describe-subnets` and pass
# it via --min-free-ips.
[[ "$MIN_FREE_IPS" =~ ^[0-9]+$ ]] || die "--min-free-ips must be a non-negative integer, got '${MIN_FREE_IPS}'"

# Report which floor this run uses; the pass/fail logic is the same either way.
if (( MIN_FREE_IPS_EXPLICIT )); then
    info "capacity floor ${MIN_FREE_IPS} free address(es) per subnet (DERIVED — passed explicitly)"
else
    info "capacity floor ${MIN_FREE_IPS} free address(es) per subnet: UNDERIVED DEFAULT, known to be understated."
    info "  8 is the internal ALB's own scaling budget and nothing else's. The interface-endpoint ENIs (one"
    info "  per service per subnet) and the Lambda Hyperplane ENIs draw from the same pool and are NOT inside"
    info "  it, so a subnet at exactly 8 free passes this check and still starves the ALB — intermittent 5xx"
    info "  and timeouts under load, behind a green apply. The ALB has landed, so this floor is now too low."
    info "  Derive it from a real \`aws ec2 describe-subnets --query 'Subnets[0].AvailableIpAddressCount'\` and"
    info "  pass --min-free-ips explicitly. See docs/runbook.md."
fi

for subnet in "${SUBNETS[@]}"; do
    read -r cidr free < <(aws ec2 describe-subnets "${AWS_ARGS[@]}" --subnet-ids "$subnet" \
        --query 'Subnets[0].[CidrBlock,AvailableIpAddressCount]' --output text) \
        || die "cannot read capacity for subnet ${subnet}"
    [[ -n "$cidr" && -n "$free" && "$free" != "None" ]] \
        || die "could not read CidrBlock/AvailableIpAddressCount for ${subnet}; refusing rather than skipping the capacity check"
    info "subnet ${subnet}: ${cidr}, ${free} free address(es)"
    (( free >= MIN_FREE_IPS )) \
        || die "subnet ${subnet} (${cidr}) has only ${free} free address(es), below the floor of ${MIN_FREE_IPS}. This stack needs one interface-endpoint ENI per service per subnet plus Lambda Hyperplane ENIs that grow with concurrency, and an internal ALB would need eight on its own. Too few does not fail the apply — it produces intermittent 5xx and timeouts under load, with nothing in the plan to see it."
done

# ---------------------------------------------------------------------------
# 0c. The adopted subnets span at least two DISTINCT Availability Zones.
# ---------------------------------------------------------------------------
# Subnet ids carry no zone, so this needs an API read. The internal ALB fails to create without
# two distinct zones.
declare -a SUBNET_ZONES=()
for subnet in "${SUBNETS[@]}"; do
    zone=$(aws ec2 describe-subnets "${AWS_ARGS[@]}" --subnet-ids "$subnet" \
        --query 'Subnets[0].AvailabilityZone' --output text) \
        || die "cannot read the Availability Zone of subnet ${subnet}"
    [[ -n "$zone" && "$zone" != "None" ]] \
        || die "could not read AvailabilityZone for ${subnet}; refusing rather than skipping the distinct-zone check"
    info "subnet ${subnet}: zone ${zone}"
    SUBNET_ZONES+=("$zone")
done
DISTINCT_ZONES=$(printf '%s\n' "${SUBNET_ZONES[@]}" | sort -u | wc -l | tr -d ' ')
(( DISTINCT_ZONES >= 2 )) \
    || die "the ${#SUBNETS[@]} adopted subnet(s) span only ${DISTINCT_ZONES} Availability Zone(s) (${SUBNET_ZONES[*]}). An Application Load Balancer cannot be created in fewer than two zones — AWS refuses with \"At least two subnets in two different Availability Zones must be specified\" — and a single-zone Lambda deployment loses the function entirely when that zone is impaired. Both modules' \`length(...) >= 2\` validations pass on same-zone ids, so this is the only check that sees it."

# ---------------------------------------------------------------------------
# 1. No named route table may be the VPC's MAIN table.
# ---------------------------------------------------------------------------
# The main table serves every subnet without an explicit association, so a NAT route on it would
# reach subnets this stack does not own.
# shellcheck disable=SC2016  # the backticks are JMESPath literals, not shell expansion
MAIN_TABLE=$(aws ec2 describe-route-tables "${AWS_ARGS[@]}" \
    --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query 'RouteTables[?Associations[?Main==`true`]].RouteTableId | [0]' --output text) \
    || die "cannot enumerate route tables for ${VPC_ID}"
if [[ -z "$MAIN_TABLE" || "$MAIN_TABLE" == "None" ]]; then
    # Every VPC has a main table; not finding one means the query or permissions are wrong.
    die "could not identify the main route table for ${VPC_ID}; refusing rather than skipping the main-route-table check"
fi
info "main route table for ${VPC_ID} is ${MAIN_TABLE}"
for table in "${TABLES[@]}"; do
    [[ "$table" != "$MAIN_TABLE" ]] \
        || die "route table ${table} is the VPC's MAIN table. A NAT default route there is the default for every subnet in ${VPC_ID} without an explicit association, including ones created later by other teams. Associate our subnets with a dedicated table first."
done

# ---------------------------------------------------------------------------
# 2. Each named table's association set must be exactly our subnets.
# ---------------------------------------------------------------------------
# Compared as sorted sets, so equal counts with different members still refuse.
OURS=$(printf '%s\n' "${SUBNETS[@]}" | sort -u)
for table in "${TABLES[@]}"; do
    associated=$(aws ec2 describe-route-tables "${AWS_ARGS[@]}" --route-table-ids "$table" \
        --query 'RouteTables[0].Associations[?SubnetId!=null].SubnetId' --output json \
        | jq -r '.[]' | sort -u) \
        || die "cannot describe route table ${table}"
    if [[ -z "$associated" ]]; then
        die "route table ${table} has no explicit subnet associations. Either it is unused (so the NAT route would do nothing) or its subnets reach it as the VPC main table — both are refusals."
    fi
    extra=$(comm -13 <(printf '%s\n' "$OURS") <(printf '%s\n' "$associated") || true)
    if [[ -n "${extra//[$'\n\t ']/}" ]]; then
        printf '%s\n' "$extra" >&2
        die "route table ${table} also serves the subnet(s) above, which are not ours. The NAT default route would reach them: they become internet-capable on all ports bounded only by their own security groups, their traffic bills to our NAT at \$0.045/GB, and they lose egress at our teardown."
    fi
done

# ---------------------------------------------------------------------------
# 2b. The NAT's subnet must be IGW-routed, and must not be one of ours.
# ---------------------------------------------------------------------------
# A NAT gateway in a subnet with no internet-gateway route is created and billed but provides no
# egress.
if [[ -n "$NAT_SUBNET_ID" ]]; then
    for subnet in "${SUBNETS[@]}"; do
        [[ "$NAT_SUBNET_ID" != "$subnet" ]] \
            || die "--nat-subnet-id ${NAT_SUBNET_ID} is also in --private-subnet-ids. That subnet routes to an internet gateway, so Lambda ENIs placed in it would be directly internet-facing."
    done

    nat_owner=$(aws ec2 describe-subnets "${AWS_ARGS[@]}" --subnet-ids "$NAT_SUBNET_ID" \
        --query 'Subnets[0].VpcId' --output text) \
        || die "cannot describe the NAT subnet ${NAT_SUBNET_ID}"
    [[ "$nat_owner" == "$VPC_ID" ]] \
        || die "NAT subnet ${NAT_SUBNET_ID} belongs to ${nat_owner}, not the adopted VPC ${VPC_ID}"

    # The subnet's effective table: its explicit association, else the VPC main table.
    nat_table=$(aws ec2 describe-route-tables "${AWS_ARGS[@]}" \
        --filters "Name=association.subnet-id,Values=${NAT_SUBNET_ID}" \
        --query 'RouteTables[0].RouteTableId' --output text) || nat_table=""
    if [[ -z "$nat_table" || "$nat_table" == "None" ]]; then
        nat_table="$MAIN_TABLE"
        info "NAT subnet ${NAT_SUBNET_ID} has no explicit association; its effective table is the main table ${MAIN_TABLE}"
    fi
    # shellcheck disable=SC2016  # JMESPath literal backticks
    igw_route=$(aws ec2 describe-route-tables "${AWS_ARGS[@]}" --route-table-ids "$nat_table" \
        --query 'RouteTables[0].Routes[?DestinationCidrBlock==`0.0.0.0/0` && GatewayId!=null].GatewayId | [0]' \
        --output text) || die "cannot read routes on ${nat_table}"
    [[ -n "$igw_route" && "$igw_route" != "None" && "$igw_route" == igw-* ]] \
        || die "the NAT subnet ${NAT_SUBNET_ID} has no 0.0.0.0/0 route to an internet gateway (table ${nat_table}, found '${igw_route}'). A NAT gateway there would be created and billed while providing no egress, and the relay's JWKS fetch would fail on a TIMEOUT rather than an error."
    info "NAT subnet ${NAT_SUBNET_ID} is IGW-routed via ${igw_route} on ${nat_table}"
fi

# ---------------------------------------------------------------------------
# 3. The adopted VPC's DEFAULT security group must admit no 0.0.0.0/0 ingress.
# ---------------------------------------------------------------------------
# Reads and refuses; it never revokes. Emptying this group would strip rules from co-tenant
# resources.
DEFAULT_SG_INGRESS=$(aws ec2 describe-security-groups "${AWS_ARGS[@]}" \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=default" \
    --query 'SecurityGroups[0].IpPermissions' --output json) \
    || die "cannot describe the default security group for ${VPC_ID}"
[[ "$DEFAULT_SG_INGRESS" != "null" ]] \
    || die "no default security group found for ${VPC_ID}; refusing rather than skipping the default-security-group check"
PUBLIC_INGRESS=$(printf '%s' "$DEFAULT_SG_INGRESS" \
    | jq -r '[.[] | select((.IpRanges // []) | any(.CidrIp == "0.0.0.0/0"))] | length')
if [[ "$PUBLIC_INGRESS" != "0" ]]; then
    printf '%s\n' "$DEFAULT_SG_INGRESS" >&2
    die "the adopted VPC's default security group admits ${PUBLIC_INGRESS} ingress rule(s) from 0.0.0.0/0. This stack must not modify that group — emptying it would revoke rules from every co-tenant on it — so this is a refusal for the VPC owner to resolve, not something to work around."
fi
info "default security group has no 0.0.0.0/0 ingress"

# ---------------------------------------------------------------------------
# 3b. Private DNS on an interface endpoint needs BOTH VPC DNS attributes.
# ---------------------------------------------------------------------------
# Interface endpoints with `private_dns_enabled` need enableDnsSupport and enableDnsHostnames on the
# VPC, or CreateVpcEndpoint fails. This refuses and names the remedy; it does not modify the VPC.
for attribute in enableDnsSupport enableDnsHostnames; do
    case "$attribute" in
        enableDnsSupport) query='EnableDnsSupport.Value'; remedy_flag='--enable-dns-support' ;;
        *) query='EnableDnsHostnames.Value'; remedy_flag='--enable-dns-hostnames' ;;
    esac
    dns_value=$(aws ec2 describe-vpc-attribute "${AWS_ARGS[@]}" \
        --vpc-id "$VPC_ID" --attribute "$attribute" --query "$query" --output text) \
        || die "cannot read the ${attribute} attribute of ${VPC_ID}; refusing rather than skipping the private-DNS check"
    [[ -n "$dns_value" && "$dns_value" != "None" ]] \
        || die "could not read ${attribute} for ${VPC_ID} (got '${dns_value}'); refusing rather than treating an unreadable precondition as a satisfied one"
    [[ "$dns_value" == "True" || "$dns_value" == "true" ]] \
        || die "the adopted VPC ${VPC_ID} has ${attribute}=${dns_value}, but private DNS on an interface endpoint requires BOTH enableDnsSupport and enableDnsHostnames to be true. EC2 refuses CreateVpcEndpoint with 'Enabling private DNS requires both enableDnsSupport and enableDnsHostnames VPC attributes set to true', and with no private DNS every Lambda resolves no AWS API at all. Remedy, for the VPC owner: aws ec2 modify-vpc-attribute --vpc-id ${VPC_ID} ${remedy_flag} . Enabling it grants DNS hostnames and revokes nothing, but it is a write to an adopted VPC, so this script refuses rather than performing it."
    info "${attribute}=${dns_value}"
done

# ---------------------------------------------------------------------------
# 4. A gateway endpoint already serving one of our tables would fail the apply.
# ---------------------------------------------------------------------------
# A route table permits one gateway association per service, so an existing s3/dynamodb endpoint on
# a named table would make `aws_vpc_endpoint.gateway` fail. Endpoints tagged with
# MANAGED_SERVICE_TAG belong to this stack and are excluded; every exclusion is printed.
for service in s3 dynamodb; do
    # shellcheck disable=SC2016  # JMESPath literal backticks, not shell expansion
    endpoints_json=$(aws ec2 describe-vpc-endpoints "${AWS_ARGS[@]}" \
        --filters "Name=vpc-id,Values=${VPC_ID}" \
            "Name=service-name,Values=com.amazonaws.${RESOLVED_REGION}.${service}" \
        --query 'VpcEndpoints[?VpcEndpointType==`Gateway`].{Id:VpcEndpointId,RouteTableIds:RouteTableIds,Managed:Tags[?Key==`service`]|[0].Value}' \
        --output json) \
        || die "cannot enumerate ${service} gateway endpoints"

    ours=$(printf '%s' "$endpoints_json" \
        | jq -r --arg tag "$MANAGED_SERVICE_TAG" '.[] | select(.Managed == $tag) | .Id') \
        || die "cannot parse the ${service} gateway endpoint inventory"
    for endpoint in $ours; do
        info "ignoring ${service} gateway endpoint ${endpoint}: tagged service=${MANAGED_SERVICE_TAG}, so it is this stack's own and converges rather than colliding"
    done

    existing=$(printf '%s' "$endpoints_json" \
        | jq -r --arg tag "$MANAGED_SERVICE_TAG" '.[] | select(.Managed != $tag) | .RouteTableIds[]?' \
        | sort -u) \
        || die "cannot parse the ${service} gateway endpoint route tables"
    [[ -n "$existing" ]] || continue
    clash=$(comm -12 <(printf '%s\n' "$existing") <(printf '%s\n' "${TABLES[@]}" | sort -u) || true)
    if [[ -n "${clash//[$'\n\t ']/}" ]]; then
        printf '%s\n' "$clash" >&2
        die "an existing ${service} gateway endpoint NOT managed by this stack (no service=${MANAGED_SERVICE_TAG} tag) already serves the route table(s) above. A table permits one gateway association per service, so the apply would fail on a duplicate. Either drop those tables from vpc_private_route_table_ids or remove ${service} from gateway_endpoint_service_names."
    fi
done

# ---------------------------------------------------------------------------
# 5. The AgentCore Runtime's VPC-mode subnets, if supplied.
# ---------------------------------------------------------------------------
# AgentCore VPC mode supports only specific AZs per region, and creation fails at apply time in
# an unsupported one. A Runtime subnet must also not use the MAIN table or have an IGW route.
# The supported set is read from infra/regions.json; a region missing from it refuses.
if [[ -n "$RUNTIME_SUBNET_IDS" ]]; then
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    PYTHON="${REPO_ROOT}/.venv/bin/python3"
    [[ -x "$PYTHON" ]] || PYTHON="python3"
    az_rc=0
    SUPPORTED_AZ_IDS=$("$PYTHON" "${REPO_ROOT}/scripts/region_map.py" az-ids --region "$RESOLVED_REGION") || az_rc=$?
    case "$az_rc" in
        0) ;;
        2) die "region ${RESOLVED_REGION} is not in infra/regions.json, so its AgentCore VPC-mode supported AZs are unknown (AWS exposes no API for them). Add the region to the map before running VPC-mode preflight there." ;;
        *) die "cannot read the AgentCore VPC-mode supported AZs for ${RESOLVED_REGION} from infra/regions.json (region_map.py exit ${az_rc})" ;;
    esac
    [[ -n "$SUPPORTED_AZ_IDS" ]] || die "infra/regions.json yielded no supported AZs for ${RESOLVED_REGION}"

    IFS=',' read -r -a RUNTIME_SUBNETS <<< "$RUNTIME_SUBNET_IDS"
    (( ${#RUNTIME_SUBNETS[@]} > 0 )) || die "--runtime-subnet-ids parsed to zero ids"
    declare -a RUNTIME_TABLES=()
    [[ -n "$RUNTIME_ROUTE_TABLE_IDS" ]] && IFS=',' read -r -a RUNTIME_TABLES <<< "$RUNTIME_ROUTE_TABLE_IDS"
    info "checking ${#RUNTIME_SUBNETS[@]} Runtime subnet(s) against supported AZs {${SUPPORTED_AZ_IDS}}"

    for subnet in "${RUNTIME_SUBNETS[@]}"; do
        read -r rt_owner rt_az < <(aws ec2 describe-subnets "${AWS_ARGS[@]}" --subnet-ids "$subnet" \
            --query 'Subnets[0].[VpcId,AvailabilityZoneId]' --output text) \
            || die "cannot describe Runtime subnet ${subnet}"
        [[ "$rt_owner" == "$VPC_ID" ]] \
            || die "Runtime subnet ${subnet} belongs to ${rt_owner}, not the adopted VPC ${VPC_ID}"
        [[ -n "$rt_az" && "$rt_az" != "None" ]] \
            || die "could not read the AvailabilityZoneId of Runtime subnet ${subnet}; refusing rather than treating an unreadable precondition as satisfied"
        # shellcheck disable=SC2076  # literal-substring match against the space-delimited set is intended
        [[ " ${SUPPORTED_AZ_IDS} " == *" ${rt_az} "* ]] \
            || die "Runtime subnet ${subnet} is in AZ ${rt_az}, which AgentCore VPC mode does not support in ${RESOLVED_REGION} (supported: ${SUPPORTED_AZ_IDS}). CreateAgentRuntime/UpdateAgentRuntime would fail at apply with a green plan."

        [[ "$NAT_SUBNET_ID" != "$subnet" ]] \
            || die "Runtime subnet ${subnet} is also --nat-subnet-id: a Runtime ENI would land on the IGW-routed NAT subnet."

        rt_table=$(aws ec2 describe-route-tables "${AWS_ARGS[@]}" \
            --filters "Name=association.subnet-id,Values=${subnet}" \
            --query 'RouteTables[0].RouteTableId' --output text) || rt_table=""
        if [[ -z "$rt_table" || "$rt_table" == "None" ]]; then
            die "Runtime subnet ${subnet} has no explicit route-table association, so it uses the VPC main table ${MAIN_TABLE}. The Runtime subnet must have its OWN table (local + s3/dynamodb gateway routes, no NAT). Associate a dedicated table first."
        fi
        [[ "$rt_table" != "$MAIN_TABLE" ]] \
            || die "Runtime subnet ${subnet} uses the VPC's MAIN table ${MAIN_TABLE}. Give it a dedicated table with the gateway-endpoint routes and no NAT route."
        # With declared Runtime route tables, the subnet's effective table must be one of them.
        if (( ${#RUNTIME_TABLES[@]} > 0 )); then
            in_declared=0
            for declared in "${RUNTIME_TABLES[@]}"; do
                [[ "$rt_table" == "$declared" ]] && in_declared=1 && break
            done
            (( in_declared == 1 )) \
                || die "Runtime subnet ${subnet} uses table ${rt_table}, which is not in --runtime-route-table-ids (${RUNTIME_ROUTE_TABLE_IDS}). The gateway-endpoint associations would then be on a table the Runtime subnet does not use, leaving ECR layer pulls no S3 path."
        fi
        # shellcheck disable=SC2016  # JMESPath literal backticks, not shell expansion
        rt_igw=$(aws ec2 describe-route-tables "${AWS_ARGS[@]}" --route-table-ids "$rt_table" \
            --query 'RouteTables[0].Routes[?DestinationCidrBlock==`0.0.0.0/0` && GatewayId!=null].GatewayId | [0]' \
            --output text) || die "cannot read routes on ${rt_table}"
        if [[ -n "$rt_igw" && "$rt_igw" != "None" && "$rt_igw" == igw-* ]]; then
            die "Runtime subnet ${subnet}'s table ${rt_table} has a 0.0.0.0/0 route to internet gateway ${rt_igw}. The Runtime must have no internet path; its table carries local + gateway-endpoint routes only, never an IGW route."
        fi
        info "Runtime subnet ${subnet}: AZ ${rt_az} supported, dedicated table ${rt_table}, no IGW route"
    done
fi

info "OK — every adopted-VPC pre-apply check passed (${#SUBNETS[@]} subnet(s), ${#TABLES[@]} table(s))"
