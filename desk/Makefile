.PHONY: check
check:
	cd .. && python3 -B -c 'from desk.desk_scenarios_checks import check_timer_audit; check_timer_audit()'
	cd .. && python3 -B -m desk.desk_scenarios_trigger_distance
	cd .. && python3 -B -m desk.desk_scenarios_bracket
	cd .. && python3 -B -m desk.desk_scenarios
	cd .. && python3 -B -m desk.desk_scenarios_deliveries
	@echo 'PASS prior scenarios and timer audit pass'
