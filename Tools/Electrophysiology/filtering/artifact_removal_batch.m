function [all_cleaned, all_removed] = artifact_removal_batch(data_cell, Fs, numWorkers)
%ARTIFACT_REMOVAL_BATCH  Run artifact_removal_inVivo_VD on each channel.
%
%   [CLEANED, REMOVED] = artifact_removal_batch(DATA_CELL, FS)
%   uses as many workers as there are CPU cores (up to 16).
%
%   [CLEANED, REMOVED] = artifact_removal_batch(DATA_CELL, FS, NUMWORKERS)
%   specifies the number of workers to start or reuse.
%
%   DATA_CELL   1×N or N×1 cell array, each element a numeric vector
%   FS          scalar sample rate, Hz
%   NUMWORKERS  positive integer (default 16)
%
%   CLEANED     cell array with the same layout as DATA_CELL
%   REMOVED     numeric vector, samples removed for each channel

    %% Input validation (MATLAB R2019b +)
    arguments
        data_cell               cell                              % must be a cell array
        Fs          (1,1)       double   {mustBePositive}         % positive scalar
        numWorkers  (1,1)       double   {mustBeInteger, mustBePositive} = 16
    end

    %% Persistent parallel pool
    persistent pool
    if isempty(pool) || ~isvalid(pool) || pool.NumWorkers ~= numWorkers
        pool = gcp('nocreate');           % get current pool if it exists
        if isempty(pool) || pool.NumWorkers ~= numWorkers
            pool = parpool('local', numWorkers);  % create or recreate
        end
    end

    %% Parallel cleaning
    n = numel(data_cell);
    all_cleaned = cell(n, 1);
    all_removed = zeros(n, 1);

    parfor i = 1:n
        [all_cleaned{i}, all_removed(i)] = ...
            artifact_removal_inVivo_VD(data_cell{i}, Fs);
    end
end
