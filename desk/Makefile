.PHONY: check
check:
	cd .. && python3 -B -c 'from desk.desk_scenarios_checks import check_timer_audit; check_timer_audit()'
	cd .. && python3 -B -m desk.desk_scenarios_anchor_gate
	cd .. && python3 -B -m desk.desk_scenarios_budget_identity
	cd .. && python3 -B -m desk.desk_scenarios_journal
	cd .. && python3 -B -m desk.desk_scenarios_history
	cd .. && python3 -B -m desk.desk_scenarios_execution_history
	cd .. && python3 -B -m desk.desk_scenarios_exit_by
	cd .. && python3 -B -m desk.desk_scenarios_exit_custody
	cd .. && python3 -B -m desk.desk_scenarios_exit_upgrade
	cd .. && python3 -B -m desk.desk_scenarios_exit_deadline
	cd .. && python3 -B -m desk.desk_scenarios_exit_algo
	cd .. && python3 -B -m desk.desk_scenarios_bracket
	cd .. && python3 -B -m desk.desk_scenarios
	cd .. && python3 -B -m desk.desk_scenarios_anchor
	cd .. && python3 -B -m desk.desk_scenarios_anchor_decoding
	cd .. && python3 -B -m desk.desk_scenarios_deliveries
