### ---------- IMPORT DEPENDENCIES ----------
import numpy as np
import pandas as pd
from scipy.stats import rankdata
import scipy.sparse as sp
from joblib import Parallel, delayed
from tqdm import tqdm


### ---------- EXPORT LIST ----------
__all__ = []

# @-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-
# ------------------------------------------------------------------------------
# ---------------------------- ** DISTANCE FUNCS ** ----------------------------
# ------------------------------------------------------------------------------
# @-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-
# SA_GS_subfunctions.R
def _2d_array_vs_2d_array_corr(matrix1, matrix2):
    # Standardize the matrices by subtracting mean and dividing by std (axis=1)
    matrix1_standardized = (matrix1 - matrix1.mean(axis=1, keepdims=True)) / matrix1.std(axis=1, keepdims=True)
    matrix2_standardized = (matrix2 - matrix2.mean(axis=1, keepdims=True)) / matrix2.std(axis=1, keepdims=True)

    # Compute dot product
    dot_product = np.dot(matrix1_standardized, matrix2_standardized.T)

    # Normalize by the number of columns and the vector magnitudes
    magnitudes_matrix1 = np.linalg.norm(matrix1_standardized, axis=1, keepdims=True)
    magnitudes_matrix2 = np.linalg.norm(matrix2_standardized, axis=1, keepdims=True)

    # Compute correlation by dividing the dot product by magnitudes
    correlation_matrix = dot_product / (magnitudes_matrix1 * magnitudes_matrix2.T)

    return correlation_matrix

def _corr_distance_matrix_batch(data, batch_size=1000, verbose=True):
    if isinstance(data, pd.DataFrame):
        data = data.values

    n_samps = data.shape[0]
    sqrt_one_minus_corr_matrix = np.zeros((n_samps, n_samps))

    n_batches = int(np.ceil(n_samps/batch_size))

    for i in tqdm(range(n_batches)) if verbose else range(n_batches):
        # Determine the indices for the current batch
        batch_indices = np.arange(
            i*batch_size,
            min((i+1)*batch_size, n_samps)
        )
        # Get the current batch of data
        batch_data = data[batch_indices, :]

        # Compute correlation of the current batch with all samples
        batch_corr = _2d_array_vs_2d_array_corr(batch_data, data)
        one_min_batch_corr = 1 - batch_corr
        one_min_batch_corr[one_min_batch_corr<0] = 0

        # Compute sqrt(1 - correlation) for the batch
        batch_dist = np.sqrt(one_min_batch_corr)

        # Zero out diagonal elements where the batch is compared with itself
        submatrix = batch_dist[:,batch_indices]
        np.fill_diagonal(submatrix, 0)
        batch_dist[:,batch_indices]=submatrix

        # Store the result in the main distance matrix
        sqrt_one_minus_corr_matrix[batch_indices, :] = batch_dist

        # This ensures correct diagonal filling only where the row and column are from the same batch
        np.fill_diagonal(
            sqrt_one_minus_corr_matrix[np.ix_(batch_indices, batch_indices)], 0
        )

    return sqrt_one_minus_corr_matrix

def _corr_distance_matrix_whole(data):
    # Equivalent to the following in R: d = sqrt(1 - stats::corr(X))
    # Computing the correlation
    corr_matrix = np.corrcoef(data)
    # Calculating sqrt_one_minus_corr_matrix
    sqrt_one_minus_corr_matrix = np.sqrt(1 - corr_matrix)
    # Ensuring diagonal contains 0 values
    np.fill_diagonal(sqrt_one_minus_corr_matrix, 0)
    return(sqrt_one_minus_corr_matrix)

def _corr_distance_matrix(data, batch_size = 1000, verbose = True):
    n_samps = data.shape[0]
    if n_samps < batch_size:
        return _corr_distance_matrix_whole(data)
    else:
        return _corr_distance_matrix_batch(data, batch_size, verbose)

def __add_row_column_names_to_dist_mat(dist_mat, adata):
    # Turn into a dataframe with row and column names
    df_dist = pd.DataFrame(
        dist_mat,
        columns = adata.obs_names,
        index = adata.obs_names
    )
    return(df_dist)

def _corr_distance(adata,
                   use_reduction=True,
                   reduction_slot="X_pca",
                   key_added="corr_dist",
                   batch_size=1000,
                   verbose=True):
    if isinstance(adata, np.ndarray) or isinstance(adata, pd.DataFrame):
        return _corr_distance_matrix(adata)

    if use_reduction == False:
        # use original features
        d = _corr_distance_matrix(adata.X, batch_size, verbose)
    elif use_reduction == True:
        # use principal components
        X = adata.obsm[reduction_slot]
        d = _corr_distance_matrix(X, batch_size, verbose)
    else:
        raise ValueError("reduction must be logical.")
    d = __add_row_column_names_to_dist_mat(d, adata)
    adata.obsp[key_added] = d

# @-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-
# ------------------------------------------------------------------------------
# ---------------------------- ** KNN ARRAY FUNC ** ----------------------------
# ------------------------------------------------------------------------------
# @-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-

def __rank_column(column): return rankdata(column, method='ordinal')

def __get_top_n_indices(dist_array_ranked, knn, njobs = 1):
    # Sort each row of the input array and get the indices of the sorted elements
    if njobs == 1:
        sorted_indices = np.argsort(dist_array_ranked, axis=1)
    else:
        sorted_indices = np.array(Parallel(n_jobs=njobs)(
            delayed(np.argsort)(row) for row in dist_array_ranked
        ))

    # Slice the sorted indices to keep only the top knn indices for each row
    top_n_indices = sorted_indices[:, :knn]

    return top_n_indices

def __get_max_knn_sorted_indices_of_batch(batch_data, max_knn):
    # For each sample, sort neighbors by distance.
    batch_dist_array_ranked = np.apply_along_axis(
        __rank_column,
        axis=1,
        arr=np.array(batch_data)
    )
    return __get_top_n_indices(
        batch_dist_array_ranked,
        max_knn,
        njobs=1
    )

def __get_max_knn_sorted_indices_1_core(
    dist_df,
    max_knn,
    batch_size=1000,
    verbose = False
):
    n_samps = dist_df.shape[0]
    n_batches = int(np.ceil(n_samps/batch_size))
    max_knn_sorted_indices_array = np.zeros([n_samps, max_knn], dtype=int)

    for i in tqdm(range(n_batches)) if verbose else range(n_batches):
        # Determine the indices for the current batch
        batch_indices = np.arange(
            i*batch_size,
            min((i+1)*batch_size, n_samps)
        )
        # Get the current batch of data
        batch_data = dist_df[batch_indices, :]

        # Trim this ranking to MaxNN
        max_knn_sorted_indices_array[batch_indices,:] = \
            __get_max_knn_sorted_indices_of_batch(batch_data, max_knn)

    return max_knn_sorted_indices_array

def __get_max_knn_sorted_indices_multicore(
    dist_df,
    max_knn,
    batch_size=1000,
    njobs=4
):
    n_samps = dist_df.shape[0]
    n_batches = int(np.ceil(n_samps/batch_size))

    results = Parallel(njobs)(
        delayed(__get_max_knn_sorted_indices_of_batch)(
            dist_df[
                batch_i*batch_size:batch_i*batch_size+batch_size,:
            ],
            max_knn
        ) for batch_i in range(n_batches)
    )
    max_knn_sorted_indices_array = np.concatenate(results, axis=0)

    return max_knn_sorted_indices_array

def _get_knn_array(
    dist_df,
    max_knn,
    batch_size=1000,
    verbose=False,
    njobs=1
):
    if njobs==1:
        return __get_max_knn_sorted_indices_1_core(
            dist_df,
            max_knn,
            batch_size,
            verbose
        )
    else:
        return __get_max_knn_sorted_indices_multicore(
            dist_df,
            max_knn,
            batch_size,
            njobs
        )

# ------------------------------ ** HELPER FUNC ** -----------------------------
def _neighbors_knn(adata,
                   max_knn=101,
                   dist_slot="corr_dist",
                   key_added="knn",
                   batch_size = 1000,
                   verbose = True,
                   njobs = 1):
    if isinstance(adata, np.ndarray) or isinstance(adata, pd.DataFrame):
        return _get_knn_array(
            adata,
            max_knn,
            batch_size,
            verbose,
            njobs
        )

    # Get the distance DataFrame
    dist_df = adata.obsp[dist_slot]

    # Comptue the KNN array
    max_knn_sorted_indices_array = _get_knn_array(
        dist_df,
        max_knn,
        batch_size,
        verbose,
        njobs
    )

    # Add this to the adata
    adata.uns[key_added] = max_knn_sorted_indices_array

# @-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-
# ------------------------------------------------------------------------------
# -------------------------- ** NEIGHBOR GRAPH FUNC ** -------------------------
# ------------------------------------------------------------------------------
# @-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-@-

# ------------------------------ ** HELPER FUNC ** -----------------------------

def __get_connectivity_graph(
    max_knn_sorted_indices_array,
    knn,
    batch_size=1000,
    verbose = False
):
    num_rows = max_knn_sorted_indices_array.shape[0]

    # If no batch_size is provided, process the entire array at once
    if batch_size is None: batch_size = num_rows

    # List to store each batch's A matrix
    csr_batch_list = []

    # Process in batches
    for start in tqdm(range(0, num_rows, batch_size)) if verbose \
    else range(0, num_rows, batch_size):
        end = min(start + batch_size, num_rows)

        # Slice max_knn_sorted_indices_array to keep only the top knn columns for this batch
        B_batch = max_knn_sorted_indices_array[start:end, :knn]

        # Create row indices for the current batch
        row_indices = np.arange(start, end).reshape(-1, 1)

        # Broadcast row indices to match the shape of B_batch
        row_indices = np.tile(row_indices, (1, knn))

        # Create a zero matrix for this batch
        A_batch = np.zeros((end - start, num_rows), dtype=int)

        # Use advanced indexing to set the values in A_batch to 1 based on indices from B_batch
        A_batch[np.arange(A_batch.shape[0]).reshape(-1, 1), B_batch] = 1

        # Append the batch's A matrix as a DataFrame to the list
        csr_batch_list.append(sp.csr_matrix(A_batch))

    # Concatenate all batch results into one final DataFrame
    return sp.vstack(csr_batch_list)

# ------------------------------- ** MAIN FUNC ** ------------------------------
def _neighbors_graph(adata,
                     n_neighbors=15,
                     knn_slot='knn',
                     batch_size=1000,
                     verbose = True):

    if isinstance(adata, np.ndarray) or isinstance(adata, pd.DataFrame):
        return __get_connectivity_graph(
            adata,
            n_neighbors,
            batch_size,
            verbose
        )
    adata.obsp['connectivities'] =  __get_connectivity_graph(
        adata.uns[knn_slot],
        n_neighbors,
        batch_size,
        verbose
    )
    # adata.uns["neighbors"] = {'connectivities':adata.obsp['connectivities']}
    adata.uns["neighbors"] = {
        'connectivities_key': 'connectivities',
        'params': {'n_neighbors': n_neighbors}
    }
