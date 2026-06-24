# CVA6 cv32a60x: single clock, 500 MHz on ASAP7
set clk_name clk_i
set clk_port_name clk_i
set clk_period 2000

# Use https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/master/flow/platforms/asap7/constraints.sdc
source $env(PLATFORM_DIR)/constraints.sdc