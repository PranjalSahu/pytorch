import torch
from functools import reduce
from torch.optim.optimizer import Optimizer


class LBFGSNEW(Optimizer):
    """Implements L-BFGS algorithm.

    .. warning::
        This optimizer doesn't support per-parameter options and parameter
        groups (there can be only one).

    .. warning::
        Right now all parameters have to be on a single device. This will be
        improved in the future.

    .. note::
        This is a very memory intensive optimizer (it requires additional
        ``param_bytes * (history_size + 1)`` bytes). If it doesn't fit in memory
        try reducing the history size, or use a different algorithm.

    Arguments:
        lr (float): learning rate (default: 1)
        max_iter (int): maximal number of iterations per optimization step
            (default: 20)
        max_eval (int): maximal number of function evaluations per optimization
            step (default: max_iter * 1.25).
        tolerance_grad (float): termination tolerance on first order optimality
            (default: 1e-5).
        tolerance_change (float): termination tolerance on function
            value/parameter changes (default: 1e-9).
        history_size (int): update history size (default: 100).
    """

    def __init__(self, params, lr=1, max_iter=20, max_eval=None,
                 tolerance_grad=1e-5, tolerance_change=1e-9, history_size=100,
                 line_search_fn=None):
        if max_eval is None:
            max_eval = max_iter * 5 // 4
        defaults = dict(lr=lr, max_iter=max_iter, max_eval=max_eval,
                        tolerance_grad=tolerance_grad, tolerance_change=tolerance_change,
                        history_size=history_size, line_search_fn=line_search_fn)
        super(LBFGSNEW, self).__init__(params, defaults)
        
        self.cuda_device =  torch.device('cuda:0')
        
        if len(self.param_groups) != 1:
            raise ValueError("LBFGS doesn't support per-parameter options "
                             "(parameter groups)")
        self.current_step = 0
        self._params = self.param_groups[0]['params']
        self._numel_cache = None

    def _numel(self):
        if self._numel_cache is None:
            self._numel_cache = reduce(lambda total, p: total + p.numel(), self._params, 0)
        return self._numel_cache

    def _gather_flat_grad(self):
        views = []
        for p in self._params:
            if p.grad is None:
                view = p.data.new(p.data.numel()).zero_()
            elif p.grad.data.is_sparse:
                view = p.grad.data.to_dense().view(-1)
            else:
                view = p.grad.data.view(-1)
            views.append(view)
        return torch.cat(views, 0)

    def _add_grad(self, step_size, update):
        offset = 0
        #print('Pranjal: update shape is ', update.shape)
        for p in self._params:
            numel = p.numel()
            # view as to avoid deprecated pointwise semantics
            p.data.add_(step_size, update[offset:offset + numel].view_as(p.data))
            offset += numel
        assert offset == self._numel()

    def step(self, closure):
        """Performs a single optimization step.

        Arguments:
            closure (callable): A closure that reevaluates the model
                and returns the loss.
        """
        assert len(self.param_groups) == 1

        group    = self.param_groups[0]
        lr       = group['lr']
        max_iter = group['max_iter']
        max_eval = group['max_eval']
        tolerance_grad   = group['tolerance_grad']
        tolerance_change = group['tolerance_change']
        line_search_fn   = group['line_search_fn']
        history_size     = group['history_size']

        # NOTE: LBFGS has only global state, but we register it as state for
        # the first param, because this helps with casting in load_state_dict
        state = self.state[self._params[0]]
        state.setdefault('func_evals', 0)
        state.setdefault('n_iter', 0)

        
        # tensors cached in state (for tracing)
        d = state.get('d')
        t = state.get('t')
        old_dirs = state.get('old_dirs')
        old_stps = state.get('old_stps')
        H_diag   = state.get('H_diag')
        prev_flat_grad = state.get('prev_flat_grad')
        prev_loss      = state.get('prev_loss')


        #Pranjal: moved this code here
        #Pranjal: get gradient for current epsilon with previous parameters first
        current_evals        = 1
        state['func_evals'] += 1
        
        #Pranjal flat_grad_old contains the gradient for previous x with previous epsilon
        loss      = float(closure())
        orig_loss = loss
        flat_grad_old = self._gather_flat_grad()
        abs_grad_sum  = flat_grad_old.abs().sum()

        if abs_grad_sum <= tolerance_grad:
            return loss
        
        #Pranjal: Use previous iteration calculated d and t and update the parameters
        # if self.current_step == 0:
        #     ########################################
        #     # This is without SGD
        #     '''
        #     #d = torch.mul(flat_grad_old, 0)
        #     #t = lr
        #     '''
            
        #     #This is with SGD
            
        #     loss = float(closure())
        #     temp_grad = self._gather_flat_grad() 
        #     d = temp_grad.neg()
        #     t = min(1., 1. / abs_grad_sum) * lr
        #     #self._add_grad(t, d)
        #     self.current_step += 1
        #     state['d'] = d
        #     state['t'] = t
        #     H_diag = 1;
        #     state['n_iter'] += 1;
        #     old_dirs = []
        #     old_stps = []
        #     print('Pranjal: First step with SGD is done')
        #     return loss

        # self.current_step += 1 
        # self._add_grad(t, d)

        #Pranjal: Get the gradients with previous epsilon
        if state['n_iter'] > 1:
            self._add_grad(t, d)
            orig_loss = float(closure())
            flat_grad = self._gather_flat_grad()
            loss = float(orig_loss)

        n_iter = 0
        # optimize for a max of max_iter iterations
        while n_iter < max_iter:
            # keep track of nb of iterations
            n_iter += 1
            state['n_iter'] += 1

            ############################################################
            # compute gradient descent direction
            ############################################################
            if state['n_iter'] == 1:
                flat_grad = self._gather_flat_grad()
                d        = flat_grad.neg()
                old_dirs = []
                old_stps = []
                H_diag   = 1
                print('Pranjal: First step with SGD is done')
            else:
                # do lbfgs update (update memory)
                y  = flat_grad.sub(flat_grad_old)
                s  = d.mul(t)  # Pranjal: this is same as (x_k  -  x_k-1)
                ys = y.dot(s)  # y*s
                
                if ys > 1e-10:
                    # updating memory
                    if len(old_dirs) == history_size:
                        # shift history by one (limited-memory)
                        old_dirs.pop(0)
                        old_stps.pop(0)
                    
                    # update scale of initial Hessian approximation
                    # Pranjal: need to add a constant delta here for taking max element wise probably ????
                    delta          = torch.tensor([1], dtype=torch.float, device=self.cuda_device)
                    #temp_delta     = torch.max(ys/ y.dot(y), delta)
                    #temp_delta = ys / y.dot(y)

                    #H_diag         = temp_delta.pow(-1)
                    #H_diag_inverse = torch.ones_like(y)*temp_delta


                    gamma     = torch.max(y.dot(y) / ys, delta)
                    #gamma = y.dot(y)/ys;
                    #H_diag    = torch.ones_like(y) / gamma;
                    H_diag     = 1 / gamma;
                    print ('VuVu - ', float(y.dot(y)), float(ys));
                    H_diag_inverse = torch.div(torch.ones_like(y), H_diag);


                    # Pranjal: adding the code for calculating value of theta
                    temp_value = s.dot(torch.mul(H_diag_inverse, s))
                    if y.dot(s) < 0.25*temp_value:
                        theta = (0.75*temp_value)/(temp_value - y.dot(s))
                    else:
                        theta = 1

                    # Pranjal: code for calculating value of y_bar using value of theta
                    y_bar = theta*y + (1-theta)*(torch.mul(H_diag_inverse, s))

                    # store new direction/step
                    # Pranjal: change of code here to use damped algorithm which uses y_bar instead of y
                    old_dirs.append(y_bar)
                    old_stps.append(s)


                # compute the approximate (L-BFGS) inverse Hessian
                # multiplied by the gradient
                num_old = len(old_dirs)

                if 'ro' not in state:
                    state['ro'] = [None] * history_size
                    state['al'] = [None] * history_size
                ro = state['ro']
                al = state['al']

                # Calculating ro = 1 / yk * sk
                # Pranjal: make old_dirs as y_k_bar as shown in the paper

                for i in range(num_old):
                    ro[i] = 1. / old_dirs[i].dot(old_stps[i])

                # iteration in L-BFGS loop collapsed to use just one buffer

                # First loop of wikipedia algo page
                # al is alpha variable
                # Pranjal: the loop which calculates mu in the paper

                q = flat_grad.neg()
                for i in range(num_old - 1, -1, -1):
                    al[i] = old_stps[i].dot(q) * ro[i]
                    q.add_(-al[i], old_dirs[i])


                # multiply by initial Hessian
                # r/d is the final direction
                # Second loop of wikipedia algo page

                d = r = torch.mul(q, H_diag)
                for i in range(num_old):
                    be_i = old_dirs[i].dot(r) * ro[i]
                    r.add_(al[i] - be_i, old_stps[i])
                print('Pranjal: Inside the main code')

            if prev_flat_grad is None:
                prev_flat_grad = flat_grad.clone()
            else:
                prev_flat_grad.copy_(flat_grad)
            prev_loss = loss

            ############################################################
            # compute step length
            ############################################################
            # reset initial guess for step size
            if state['n_iter'] == 1:
                t = min(1., 1. / abs_grad_sum) * lr
            else:
                t = lr

            # directional derivative
            gtd = flat_grad.dot(d)  # g * d
            print('Pranjal Debug values: ', float(flat_grad.abs().sum()), float(d.abs().sum()), float(gtd), float(H_diag))
            
            # optional line search: user function
            ls_func_evals = 0
            if line_search_fn is not None:
                # perform line search, using user function
                raise RuntimeError("line search function is not supported yet")
            else:
                # no line search, simply move with fixed-step
                #state['prev_direction_to_apply'] = d
                #state['prev_step_to_apply']      = t

                #self._add_grad(t, d)
                if n_iter != max_iter:
                    # re-evaluate function only if not in last iteration
                    # the reason we do this: in a stochastic setting,
                    # no use to re-evaluate that function here
                    loss   = float(closure())
                    flat_grad     = self._gather_flat_grad()
                    abs_grad_sum  = flat_grad.abs().sum()
                    ls_func_evals = 1

            # update func eval
            current_evals       += ls_func_evals
            state['func_evals'] += ls_func_evals

            ############################################################
            # check conditions
            ############################################################
            if n_iter == max_iter:
                print('Pranjal n_iter break')
                break

            if current_evals >= max_eval:
                print('Pranjal current_evals break') 
                break

            if abs_grad_sum <= tolerance_grad:
                print('Pranjal abs_grad_sum break')
                break

            if gtd > -tolerance_change:
                print('Pranjal gtd break')
                break

            if d.mul(t).abs_().sum() <= tolerance_change:
                print('Pranjal d mul abs break')
                break

            if abs(loss - prev_loss) < tolerance_change:
                print('Pranjal loss prev_loss break') 
                break

        state['d'] = d
        state['t'] = t
        state['old_dirs'] = old_dirs
        state['old_stps'] = old_stps
        state['H_diag']   = H_diag
        state['prev_flat_grad'] = prev_flat_grad
        state['prev_loss']      = prev_loss

        return orig_loss
