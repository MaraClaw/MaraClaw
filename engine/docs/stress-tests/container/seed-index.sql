-- Rebuilds the saved secondary index :name; stress.sh runs one of these per index in parallel.
SELECT def FROM stress_index_defs WHERE name = :'name' \gexec
