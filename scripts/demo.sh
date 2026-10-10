#!/usr/bin/env bash
# Walk the whole API in one go: create a loan, pay it, retry the same key, and
# fire concurrent payments. Prints what came back at each step.
#
#   docker compose up -d --build
#   bash scripts/demo.sh
#
# Needs curl and python3 (for readable JSON only).
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"

show() { python3 -m json.tool 2>/dev/null || cat; }
key()  { python3 -c "import uuid;print(uuid.uuid4())"; }

echo "waiting for ${BASE}"
for _ in $(seq 1 60); do
  curl -fsS "${BASE}/openapi.json" >/dev/null 2>&1 && break
  sleep 1
done

echo
echo "1. create a loan: 1200.00 at 12% APR over 6 monthly installments"
LOAN=$(curl -fsS -X POST "${BASE}/loans" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(key)" \
  -d '{"principal_cents":120000,"apr_bps":1200,"installment_count":6,
       "cadence":"monthly","start_date":"2026-01-15"}')
echo "${LOAN}" | show
LOAN_ID=$(echo "${LOAN}" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")

echo
echo "   installments sum exactly to the total payable:"
echo "${LOAN}" | python3 -c "
import json,sys
d=json.load(sys.stdin)
total=sum(i['amount_cents'] for i in d['installments'])
print(f\"   total_cents={d['total_cents']}  sum(installments)={total}  equal={total==d['total_cents']}\")"

echo
echo "2. pay 200.00"
PAY_KEY=$(key)
curl -fsS -X POST "${BASE}/loans/${LOAN_ID}/payments" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${PAY_KEY}" \
  -d '{"amount_cents":20000}' | show

echo
echo "3. retry the SAME idempotency key: the stored result comes back, no second payment"
curl -fsS -X POST "${BASE}/loans/${LOAN_ID}/payments" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${PAY_KEY}" \
  -d '{"amount_cents":20000}' | show

echo
echo "4. eight concurrent payments of 10.00, each with its own key"
for _ in $(seq 1 8); do
  curl -fsS -X POST "${BASE}/loans/${LOAN_ID}/payments" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $(key)" \
    -d '{"amount_cents":1000}' -o /dev/null &
done
wait
echo "   done"

echo
echo "5. final state"
curl -fsS "${BASE}/loans/${LOAN_ID}" | python3 -c "
import json,sys
d=json.load(sys.stdin)
paid=sum(i['paid_cents'] for i in d['installments'])
print(f\"   status            {d['status']}\")
print(f\"   total_cents       {d['total_cents']}\")
print(f\"   paid_cents        {d['paid_cents']}\")
print(f\"   outstanding_cents {d['outstanding_cents']}\")
print(f\"   paid + outstanding == total : {d['paid_cents'] + d['outstanding_cents'] == d['total_cents']}\")
print(f\"   installment paid sums match : {paid == d['paid_cents']}\")
print(f\"   no installment overpaid     : {all(0 <= i['paid_cents'] <= i['amount_cents'] for i in d['installments'])}\")"

echo
echo "6. an overpayment is refused with a stable error code"
curl -sS -X POST "${BASE}/loans/${LOAN_ID}/payments" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(key)" \
  -d '{"amount_cents":99999999}' | show
