.PHONY: check
check:
	cd .. && python3 -B -c 'from desk.desk_scenarios_checks import check_timer_audit; check_timer_audit()'
	cd .. && python3 -B -m desk.desk_scenarios
	cd .. && python3 -B -m desk.desk_scenarios_anchor
	cd .. && python3 -B -m desk.desk_scenarios_anchor_decoding
